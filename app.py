# app_spo2.py
# DN pro - SpO2 (E-based) | step = 1 minute
# Spec-locked behavior:
# - Encode E from raw SpO2 (good=100, bad=88, clip T in [0,1]) then compute vE/Drop
# - ON* if E==0 (LIMIT_E0)
# - ON if Drop > s (DROP_EVENT), s=0.30
# - ON if VERY_LOW streak hits 3 (VERY_LOW_PERSIST)
# - ON if LOW streak hits 5 (LOW_PERSIST)
# - Reset streaks when E >= E_RESET (0.5556) or E==0
# - OFF otherwise (NO_TRIGGER). 'flat' note if |vE|<=p, p=0.01
# Output has no counters.

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

# E thresholds (locked numeric per your spec)
E_RESET = 0.5556      # reset persistence when E >= this (≈ E at SpO2=92)
E_VLOW = 0.1597       # very-low boundary (≈ E at SpO2=89)
E_LOW_MAX = 0.4375    # low boundary (≈ E at SpO2=91)

N_VLOW = 3
N_LOW = 5

MAX_POINTS = 100
EPS = 1e-9  # numeric stability for comparisons


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

    # Persistence counters (internal only; NOT exported)
    # low_streak counts minutes where 0 < E <= E_LOW_MAX
    # vlow_streak counts minutes where 0 < E <= E_VLOW
    low_streak = 0
    vlow_streak = 0

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

        # ---------- Reset rules ----------
        # Reset persistence when reserve recovers (E >= E_RESET)
        if e >= E_RESET - EPS:
            low_streak = 0
            vlow_streak = 0
            note_parts.append("reset (E>=E_reset)")

        # Reset also when LIMIT (E==0): new episode boundary
        if e <= 0.0 + EPS:
            low_streak = 0
            vlow_streak = 0

        # ---------- Update streaks (E-based) ----------
        # Only count persistence in low/very-low when E is above 0 (limit handled separately)
        if e > 0.0 + EPS:
            # LOW band for 5-min rule: 0 < E <= E_LOW_MAX
            if e <= E_LOW_MAX + EPS:
                low_streak += 1
            else:
                low_streak = 0

            # VERY-LOW band for 3-min rule: 0 < E <= E_VLOW
            if e <= E_VLOW + EPS:
                vlow_streak += 1
            else:
                vlow_streak = 0
        else:
            # at limit
            low_streak = 0
            vlow_streak = 0

        # Optional note (no counters shown)
        if e > 0.0 + EPS:
            if e <= E_VLOW + EPS and vlow_streak < N_VLOW:
                note_parts.append("counting very-low persistence")
            elif e <= E_LOW_MAX + EPS and low_streak < N_LOW:
                note_parts.append("counting low persistence")

        # ---------- Decide Alert/Reason (priority) ----------
        alert = "OFF"
        reason = "NO_TRIGGER"

        # Priority 1: LIMIT_E0 (E == 0)
        if e <= 0.0 + EPS:
            alert = "ON*"
            reason = "LIMIT_E0"

        # Priority 2: DROP_EVENT (Drop > s)
        elif drop is not None and drop > S_DROP:
            alert = "ON"
            reason = "DROP_EVENT"

        # Priority 3: VERY_LOW_PERSIST trigger (exact hit == 3)
        elif vlow_streak == N_VLOW:
            alert = "ON"
            reason = "VERY_LOW_PERSIST"

        # Priority 4: LOW_PERSIST trigger (exact hit == 5)
        elif low_streak == N_LOW:
            alert = "ON"
            reason = "LOW_PERSIST"

        # OFF otherwise
        # Note already includes 'flat' or 'counting...' or 'reset...'

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
    # round for display only
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

    default_line = "92,91,90,89,88,90,91,92"
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

    st.write(f"Parsed **{len(series)}** points (each point = **1 minute**).")

    df = compute_table(series)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### Notes (E-based)")
    st.markdown(
        f"""
- **Reason codes (locked):** `LIMIT_E0`, `DROP_EVENT`, `VERY_LOW_PERSIST`, `LOW_PERSIST`, `NO_TRIGGER`
- **ON\\***: `LIMIT_E0` when **E = 0**.
- **ON**: `DROP_EVENT` when **Drop > s** (s = {S_DROP:.2f}).
- **ON**: `VERY_LOW_PERSIST` when **E ≤ {E_VLOW:.4f}** persists to the **3rd consecutive step** (trigger-only at the hit).
- **ON**: `LOW_PERSIST` when **0 < E ≤ {E_LOW_MAX:.4f}** persists to the **5th consecutive step** (trigger-only at the hit).
- Persistence resets when **E ≥ {E_RESET:.4f}** or **E = 0**.
- **OFF**: `NO_TRIGGER`; `flat` in Note indicates **|vE| ≤ p** (p = {P_FLAT:.2f}).
"""
    )


if __name__ == "__main__":
    main()
