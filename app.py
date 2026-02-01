# app_spo2.py
# DN pro - SpO2 (E-based) | step = 1 minute
# UI: comma-separated input + button to generate output

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

E_RESET = 0.5556
E_VLOW = 0.1597
E_LOW_MAX = 0.4375
N_VLOW = 3
N_LOW = 5

MAX_POINTS = 100


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


def parse_spo2_series(text: str) -> List[int]:
    """
    Parse input as integers. Supports comma-separated primarily, also spaces/newlines.
    Keeps at most MAX_POINTS numbers.
    """
    if not text.strip():
        return []
    tokens = re.split(r"[,\s;]+", text.strip())
    vals: List[int] = []
    for tok in tokens:
        if not tok:
            continue
        if not re.fullmatch(r"[+-]?\d+", tok):
            continue
        vals.append(int(tok))
        if len(vals) >= MAX_POINTS:
            break
    return vals


def clamp_spo2(x: int) -> int:
    return max(50, min(100, x))


def spo2_to_e(spo2: int) -> float:
    """
    E-based encoding (locked):
      T = clip((100 - spo2)/12, 0, 1)
      E = 1 - T^2
    """
    s = float(clamp_spo2(spo2))
    t = (GOOD_SPO2 - s) / DENOM
    t = 0.0 if t < 0.0 else (1.0 if t > 1.0 else t)
    return 1.0 - (t * t)


def compute_table(spo2_raw: List[int]) -> pd.DataFrame:
    rows: List[RowOut] = []
    prev_e: Optional[float] = None

    # internal counters (NOT exported)
    cnt_low = 0
    cnt_vlow = 0

    for i, raw in enumerate(spo2_raw, start=1):
        e = spo2_to_e(raw)

        ve: Optional[float] = None
        drop: Optional[float] = None
        note_parts: List[str] = []

        if prev_e is not None:
            ve = e - prev_e
            drop = max(0.0, -ve)
            if abs(ve) <= P_FLAT:
                note_parts.append("flat (|vE|<=p)")
        else:
            note_parts.append("first sample")

        # ---- persistence counters (E-based) ----
        if e >= E_RESET:
            cnt_low = 0
            cnt_vlow = 0
            note_parts.append("reset (E>=E_reset)")
        else:
            if e <= E_VLOW:
                cnt_vlow += 1
                cnt_low += 1
                if cnt_vlow < N_VLOW:
                    note_parts.append("counting very-low persistence")
            elif (e > E_VLOW) and (e <= E_LOW_MAX):
                cnt_low += 1
                cnt_vlow = 0
                if cnt_low < N_LOW:
                    note_parts.append("counting low persistence")
            else:
                cnt_low = 0
                cnt_vlow = 0

        # ---- alert decision by priority ----
        alert = "OFF"
        reason = "NO_TRIGGER"

        if e <= 1e-12:
            alert = "ON*"
            reason = "LIMIT_E0"
        elif drop is not None and drop > S_DROP:
            alert = "ON"
            reason = "DROP_EVENT"
        elif cnt_vlow >= N_VLOW and e <= E_VLOW:
            alert = "ON"
            reason = "VERY_LOW_PERSIST"
        elif cnt_low >= N_LOW and (e > E_VLOW) and (e <= E_LOW_MAX):
            alert = "ON"
            reason = "LOW_PERSIST"

        rows.append(
            RowOut(
                minute=i,
                spo2=int(raw),
                e=float(e),
                ve=None if ve is None else float(ve),
                drop=None if drop is None else float(drop),
                alert=alert,
                reason=reason,
                note="; ".join(note_parts) if note_parts else "",
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

    st.caption(
        "Input SpO₂ as a comma-separated series (max 100 points). "
        "Click the button to generate the output table."
    )

    # --- input: comma-separated ---
    default_line = "92,91,90,89,88,90,91,92"
    series_text = st.text_input(
        "SpO₂ series (comma-separated):",
        value=default_line,
    )

    col1, col2 = st.columns([1, 3])
    with col1:
        run = st.button("Generate output", type="primary")
    with col2:
        st.markdown("**Note:** Each point = **1 minute**. Max **100** values.")

    if not run:
        st.info("Enter your SpO₂ series above, then click **Generate output**.")
        return

    series = parse_spo2_series(series_text)
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
    st.markdown("### Notes (E-based rules)")
    st.markdown(
        f"""
- **Reason codes:** `LIMIT_E0`, `DROP_EVENT`, `VERY_LOW_PERSIST`, `LOW_PERSIST`, `NO_TRIGGER`
- **ON\\***: `LIMIT_E0` (E reaches minimum).
- **ON**: `DROP_EVENT` when **Drop > s** (s = {S_DROP:.2f}).
- **ON**: `VERY_LOW_PERSIST` when **E ≤ {E_VLOW:.4f}** persists for **{N_VLOW}** steps.
- **ON**: `LOW_PERSIST` when **{E_VLOW:.4f} < E ≤ {E_LOW_MAX:.4f}** persists for **{N_LOW}** steps (reset if **E ≥ {E_RESET:.4f}**).
- **OFF**: `NO_TRIGGER`; `flat` in Note means **|vE| ≤ p** (p = {P_FLAT:.2f}).
"""
    )


if __name__ == "__main__":
    main()
