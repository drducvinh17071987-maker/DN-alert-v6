# app_spo2.py
# DN pro • SpO2 (E-based) — step = 1 minute
# FINAL rules: LIMIT window + reminder, DROP_EVENT window
# Input: comma-separated raw SpO2, max 100 points, click button to output.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import streamlit as st

# -------------------------
# Locked encoding
# -------------------------
GOOD = 100.0
BAD = 88.0
DEN = GOOD - BAD  # 12

# thresholds (locked)
S_DROP = 0.30
E_RESET = 0.5556
E_VERY_LOW = 0.1597
P_FLAT = 0.01

# windows (locked per your latest chốt)
LIMIT_ON_MIN = 5
LIMIT_REMIND_OFF_MIN = 10
DROP_ON_MIN = 3

MAX_POINTS = 100
EPS = 1e-9


@dataclass
class RowOut:
    minute: int
    spo2: int
    e: float
    ve: Optional[float]
    drop: Optional[float]
    alert: str
    reason: str
    note: str


def parse_csv_series(text: str) -> List[int]:
    """Parse comma/space/newline separated integers (max MAX_POINTS)."""
    if not text.strip():
        return []
    toks = re.split(r"[,\s;]+", text.strip())
    out: List[int] = []
    for t in toks:
        if not t:
            continue
        if not re.fullmatch(r"[+-]?\d+", t):
            continue
        out.append(int(t))
        if len(out) >= MAX_POINTS:
            break
    return out


def clamp_spo2(x: int) -> int:
    return max(50, min(100, x))


def spo2_to_e(spo2: int) -> float:
    """E = 1 - clip((100-spo2)/12)^2"""
    s = float(clamp_spo2(spo2))
    T = (GOOD - s) / DEN
    if T < 0.0:
        T = 0.0
    elif T > 1.0:
        T = 1.0
    return 1.0 - (T * T)


def compute_table(spo2_raw: List[int]) -> pd.DataFrame:
    rows: List[RowOut] = []
    prev_e: Optional[float] = None

    # --- LIMIT state ---
    limit_on_left = 0          # minutes left for current ON* window
    limit_cooldown_left = 0    # minutes until next reminder ON* (only if still very-low)

    # --- DROP_EVENT state ---
    drop_on_left = 0           # minutes left for ON window (DROP_EVENT)

    for i, raw in enumerate(spo2_raw, start=1):
        e = spo2_to_e(raw)

        ve: Optional[float] = None
        drop: Optional[float] = None
        note_parts: List[str] = []

        if prev_e is None:
            note_parts.append("first sample")
        else:
            ve = e - prev_e
            drop = max(0.0, prev_e - e)
            if abs(ve) <= P_FLAT + EPS:
                note_parts.append("flat (|vE|<=p)")

        # -------------------------
        # Global recovery override (OFF early)
        # -------------------------
        if e >= E_RESET - EPS:
            # Recovery cancels ongoing ON windows and reminders
            if limit_on_left > 0 or limit_cooldown_left > 0 or drop_on_left > 0:
                note_parts.append("recovery: cancel ON/reminder")
            limit_on_left = 0
            limit_cooldown_left = 0
            drop_on_left = 0

        # -------------------------
        # Trigger logic (priority)
        # -------------------------

        # 1) LIMIT trigger: E==0 starts ON* window (5 min) and schedules reminder cooldown (10 min)
        if e <= 0.0 + EPS and limit_on_left == 0:
            limit_on_left = LIMIT_ON_MIN
            limit_cooldown_left = LIMIT_REMIND_OFF_MIN
            note_parts.append("limit: start ON* window")

        # 2) DROP_EVENT trigger: Drop > s starts ON window (3 min)
        # (only if not in hard recovery-cancelled state)
        if drop is not None and drop > S_DROP:
            drop_on_left = DROP_ON_MIN
            note_parts.append("drop_event: start ON window")

        # 3) LIMIT reminder: after OFF, if still very-low, re-notify every 10 min with 5-min ON* window
        # Condition: not currently in limit ON window, cooldown expired, and E still very-low.
        if limit_on_left == 0 and limit_cooldown_left == 0 and e <= E_VERY_LOW + EPS:
            limit_on_left = LIMIT_ON_MIN
            limit_cooldown_left = LIMIT_REMIND_OFF_MIN
            note_parts.append("limit reminder: start ON* window")

        # -------------------------
        # Decide Alert/Reason for this minute
        # -------------------------
        alert = "OFF"
        reason = "NO_TRIGGER"

        # LIMIT window dominates presentation (ON*)
        if limit_on_left > 0:
            alert = "ON*"
            # If this window started by an E==0 observation this minute, label LIMIT_ON
            # Otherwise it may be a reminder window; but both are safe to call LIMIT_ON in UI.
            reason = "LIMIT_ON"
        elif drop_on_left > 0:
            alert = "ON"
            reason = "DROP_EVENT"
        else:
            alert = "OFF"
            reason = "NO_TRIGGER"

        # -------------------------
        # Decrement timers AFTER emitting current state
        # -------------------------
        if limit_on_left > 0:
            limit_on_left -= 1
        else:
            # only decrement cooldown when not actively ON* (i.e., during mute interval)
            if limit_cooldown_left > 0 and e <= E_VERY_LOW + EPS:
                limit_cooldown_left -= 1
            # If not very-low anymore, freeze cooldown at 0 (no reminder needed)
            if e > E_VERY_LOW + EPS:
                limit_cooldown_left = 0

        if drop_on_left > 0:
            drop_on_left -= 1

        # display rounding
        e_disp = round(e, 4)
        ve_disp = None if ve is None else round(ve, 4)
        drop_disp = None if drop is None else round(drop, 4)

        rows.append(
            RowOut(
                minute=i,
                spo2=int(raw),
                e=e_disp,
                ve=ve_disp,
                drop=drop_disp,
                alert=alert,
                reason=reason,
                note="; ".join(note_parts),
            )
        )

        prev_e = e

    df = pd.DataFrame([r.__dict__ for r in rows])
    return df.rename(
        columns={
            "minute": "Minute",
            "spo2": "SpO2",
            "e": "E",
            "ve": "vE",
            "drop": "Drop",
            "alert": "Alert",
            "reason": "Reason",
            "note": "Note",
        }
    )


def main() -> None:
    st.set_page_config(page_title="DN pro • SpO2 (E-based)", layout="wide")
    st.title("DN pro • SpO₂ (E-based) — 1 step = 1 minute")

    st.caption("Input raw SpO₂ as a comma-separated series (max 100). Click the button to generate output.")

    default_line = "93,92,91,90,89,90,91,92,89,90,91,92,88,89,89,92"
    series_text = st.text_input("SpO₂ series (comma-separated):", value=default_line)

    col1, col2 = st.columns([1, 3])
    with col1:
        run = st.button("Generate output", type="primary")
    with col2:
        st.markdown("**Each point = 1 minute.** Max **100** values.")

    if not run:
        st.info("Enter your SpO₂ series above, then click **Generate output**.")
        return

    series = parse_csv_series(series_text)
    if not series:
        st.error("No valid numbers found. Example: 92,91,90,89,88,90,91,92")
        return

    if len(series) > MAX_POINTS:
        series = series[:MAX_POINTS]
        st.warning(f"Input exceeded {MAX_POINTS} points; only the first {MAX_POINTS} were used.")

    df = compute_table(series)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### How to read the table (locked rules)")
    st.markdown(
        f"""
- **Mapping (locked):** encode raw SpO₂ → **E**, then compute **vE** and **Drop**.
- **Reason codes:** `LIMIT_ON`, `DROP_EVENT`, `NO_TRIGGER`
- **LIMIT_ON (ON\\*):** when **E = 0**, DN notifies for **{LIMIT_ON_MIN} minutes**, then mutes.  
  If the signal remains in **very-low** (E ≤ {E_VERY_LOW:.4f}), DN sends a reminder: **ON\\*** again after **{LIMIT_REMIND_OFF_MIN} minutes**, for **{LIMIT_ON_MIN} minutes**.
- **DROP_EVENT (ON):** when **Drop > s** (s = {S_DROP:.2f}), DN notifies for **{DROP_ON_MIN} minutes**, then mutes.  
  DN does **not** re-notify unless a new Drop event occurs or LIMIT occurs.
- **Recovery OFF early:** if **E ≥ {E_RESET:.4f}**, ongoing ON windows and reminders are cancelled.
- `flat` in Note indicates **|vE| ≤ p** (p = {P_FLAT:.2f}).
- **Very-low vs low (avoid confusion):** this app only uses **very-low threshold** (E ≤ {E_VERY_LOW:.4f}) for LIMIT reminders.  
  “Low” is **not** used in this version, so there is no risk of swapping low/very-low rules.
"""
    )


if __name__ == "__main__":
    main()
