# app_spo2.py
# DN pro - SpO2 (E-based) | step = 1 minute
# Locked mapping: good=100, bad=88, T clipped to [0,1], E = 1 - T^2
#
# ON/OFF rules (E-based):
# 1) LIMIT_E0: if E == 0 -> Alert = ON* (highest priority)
# 2) DROP_EVENT: if Drop > s -> Alert = ON
# 3) VERY_LOW_PERSIST: if very-low streak hits 3 -> Alert = ON, then HOLD ON for 5 minutes
# 4) LOW_PERSIST: if low streak hits 5 -> Alert = ON, then HOLD ON for 3 minutes
# 5) Otherwise OFF (NO_TRIGGER). Note 'flat' if |vE|<=p.
#
# Persistence reset when E >= E_RESET or when E == 0.
# Early OFF: if hold is active and E leaves its band or E>=E_RESET.

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import streamlit as st

# -------------------------
# Locked parameters (SpO2)
# -------------------------
GOOD_SPO2 = 100.0
BAD_SPO2 = 88.0
DENOM = (GOOD_SPO2 - BAD_SPO2)  # 12.0

S_DROP = 0.30  # Drop > s => ON (DROP_EVENT)
P_FLAT = 0.01  # |vE| <= p => flat note

# E thresholds (locked numeric per spec)
E_RESET = 0.5556      # reset persistence when E >= this (≈ E at SpO2=92)
E_VLOW = 0.1597       # very-low boundary (≈ E at SpO2=89)
E_LOW_MAX = 0.4375    # low max boundary (≈ E at SpO2=91)

# Trigger streak lengths (locked per spec)
N_VLOW = 3
N_LOW = 5

# Hold lengths (per your latest correction: very-low holds longer than low)
HOLD_VLOW = 5  # minutes
HOLD_LOW = 3   # minutes

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


def parse_series_csv(text: str) -> List[int]:
    """Parse comma/space/newline separated integers (max MAX_POINTS)."""
    if not text.strip():
        return []
    tokens = re.split(r"[,\s;]+", text.strip())
    out: List[int] = []
    for tok in tokens:
        if not tok:
            continue
        if not re.fullmatch(r"[+-]?\d+", tok):
            continue
        out.append(int(tok))
        if len(out) >= MAX_POINTS:
            break
    return out


def clamp_spo2(x: int) -> int:
    return max(50, min(100, x))


def spo2_to_e(spo2: int) -> float:
    """
    T = clip((100 - SpO2)/12, 0, 1)
    E = 1 - T^2
    """
    s = float(clamp_spo2(spo2))
    t = (GOOD_SPO2 - s) / DENOM
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    return 1.0 - (t * t)


def compute_table(spo2_raw: List[int]) -> pd.DataFrame:
    rows: List[RowOut] = []
    prev_e: Optional[float] = None

    # Internal persistence streak counters (NOT exported)
    low_streak = 0
    vlow_streak = 0

    # Hold state (NOT exported)
    hold_left = 0            # minutes left to keep ON
    hold_reason = ""         # "VERY_LOW_PERSIST" or "LOW_PERSIST"

    for i, raw in enumerate(spo2_raw, start=1):
        e = spo2_to_e(raw)

        ve: Optional[float] = None
        drop: Optional[float] = None
        note_parts: List[str] = []

        if prev_e is None:
            note_parts.append("first sample")
        else:
            ve = e - prev_e
            drop = max(0.0, -ve)
            if abs(ve) <= P_FLAT + EPS:
                note_parts.append("flat (|vE|<=p)")

        # -------------------------
        # Priority 1: LIMIT_E0 (E==0) -> ON* immediate
        # -------------------------
        if e <= 0.0 + EPS:
            # Reset all internal states (new episode)
            low_streak = 0
            vlow_streak = 0
            hold_left = 0
            hold_reason = ""

            rows.append(
                RowOut(
                    minute=i,
                    spo2=int(raw),
                    e=float(e),
                    ve=None if ve is None else float(ve),
                    drop=None if drop is None else float(drop),
                    alert="ON*",
                    reason="LIMIT_E0",
                    note="; ".join(note_parts),
                )
            )
            prev_e = e
            continue

        # -------------------------
        # Reset rule on recovery
        # -------------------------
        if e >= E_RESET - EPS:
            low_streak = 0
            vlow_streak = 0
            if hold_left > 0:
                note_parts.append("hold cancelled (E>=E_reset)")
            hold_left = 0
            hold_reason = ""
            note_parts.append("reset (E>=E_reset)")

        # -------------------------
        # Update streaks (E-based)
        # -------------------------
        # LOW band: 0 < E <= E_LOW_MAX
        if e <= E_LOW_MAX + EPS:
            low_streak += 1
        else:
            low_streak = 0

        # VERY-LOW band: 0 < E <= E_VLOW
        if e <= E_VLOW + EPS:
            vlow_streak += 1
        else:
            vlow_streak = 0

        # optional note (no counters)
        if e <= E_VLOW + EPS and vlow_streak < N_VLOW:
            note_parts.append("counting very-low persistence")
        elif (e > E_VLOW + EPS) and (e <= E_LOW_MAX + EPS) and low_streak < N_LOW:
            note_parts.append("counting low persistence")

        # -------------------------
        # Early OFF for hold if reserve leaves the band
        # -------------------------
        if hold_left > 0:
            if hold_reason == "VERY_LOW_PERSIST" and e > E_VLOW + EPS:
                note_parts.append("hold ended early (E>very-low)")
                hold_left = 0
                hold_reason = ""
            elif hold_reason == "LOW_PERSIST" and e > E_LOW_MAX + EPS:
                note_parts.append("hold ended early (E>low)")
                hold_left = 0
                hold_reason = ""

        # -------------------------
        # Decide Alert/Reason
        # -------------------------
        alert = "OFF"
        reason = "NO_TRIGGER"
        triggered_now = False

        # Priority 2: DROP_EVENT
        if drop is not None and drop > S_DROP:
            alert = "ON"
            reason = "DROP_EVENT"
            triggered_now = True

        # Priority 3: VERY_LOW_PERSIST trigger at the HIT moment (==3)
        elif vlow_streak == N_VLOW:
            hold_left = HOLD_VLOW
            hold_reason = "VERY_LOW_PERSIST"
            alert = "ON"
            reason = "VERY_LOW_PERSIST"
            triggered_now = True

        # Priority 4: LOW_PERSIST trigger at the HIT moment (==5), only when not very-low
        elif low_streak == N_LOW and e > E_VLOW + EPS:
            hold_left = HOLD_LOW
            hold_reason = "LOW_PERSIST"
            alert = "ON"
            reason = "LOW_PERSIST"
            triggered_now = True

        # If no new trigger, but hold active -> keep ON
        if not triggered_now and hold_left > 0:
            alert = "ON"
            reason = hold_reason
            note_parts.append(f"holding ON ({hold_left} min left)")

        # If ON due to hold, decrement hold after emitting ON
        if alert == "ON" and reason in ("VERY_LOW_PERSIST", "LOW_PERSIST") and hold_left > 0:
            hold_left -= 1
            if hold_left == 0:
                note_parts.append("hold completed -> next OFF unless retrigger")

        rows.append(
            RowOut(
                minute=i,
                spo2=int(raw),
                e=float(e),
                ve=None if ve is None else float(ve),
                drop=None if drop is None else float(drop),
                alert=alert,
                reason=reason,
                note="; ".join(note_parts),
            )
        )

        prev_e = e

    df = pd.DataFrame([r.__dict__ for r in rows])
    df["e"] = df["e"].round(4)
    df["ve"] = df["ve"].round(4)
    df["drop"] = df["drop"].round(4)

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

    st.caption("Enter a comma-separated SpO₂ series (max 100 points). Click the button to generate output.")

    default_line = "93,91,90,89,88,89,89,91,92,89,90,91,92,91,89,90"
    series_text = st.text_input("SpO₂ series (comma-separated):", value=default_line)

    col1, col2 = st.columns([1, 3])
    with col1:
        run = st.button("Generate output", type="primary")
    with col2:
        st.markdown("**Each point = 1 minute.** Max **100** values.")

    if not run:
        st.info("Enter your SpO₂ series above, then click **Generate output**.")
        return

    series = parse_series_csv(series_text)
    if not series:
        st.error("No valid numbers found. Example: 92,91,90,89,88,90,91,92")
        return

    if len(series) > MAX_POINTS:
        series = series[:MAX_POINTS]
        st.warning(f"Input exceeded {MAX_POINTS} points; only the first {MAX_POINTS} were used.")

    df = compute_table(series)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### Notes (E-based)")
    st.markdown(
        f"""
- **Reason codes:** `LIMIT_E0`, `DROP_EVENT`, `VERY_LOW_PERSIST`, `LOW_PERSIST`, `NO_TRIGGER`
- **ON\\***: `LIMIT_E0` when **E = 0** (immediate override).
- **ON (event):** `DROP_EVENT` when **Drop > s** (s = {S_DROP:.2f}).
- **Persistence triggers:**  
  - `VERY_LOW_PERSIST` triggers when very-low streak hits **{N_VLOW}**; then holds ON for **{HOLD_VLOW} minutes** (early OFF if E leaves very-low or E≥E_reset).  
  - `LOW_PERSIST` triggers when low streak hits **{N_LOW}**; then holds ON for **{HOLD_LOW} minutes** (early OFF if E leaves low or E≥E_reset).
- `flat` in Note indicates **|vE| ≤ p** (p = {P_FLAT:.2f}).
"""
    )


if __name__ == "__main__":
    main()
