# app_spo2.py
# DN pro - SpO2 (E-based) | step = 1 minute | IP-safe UI (no formula disclosure in UI text)
# Spec locked: good=100, bad=88, s=0.30, p=0.01
# Reasons: LIMIT_E0, DROP_EVENT, VERY_LOW_PERSIST, LOW_PERSIST, NO_TRIGGER

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional, Tuple

import pandas as pd
import streamlit as st


# -------------------------
# Locked parameters (SpO2)
# -------------------------
GOOD_SPO2 = 100.0
BAD_SPO2 = 88.0  # also implies E=0 at/below this point due to clipping
DENOM = (GOOD_SPO2 - BAD_SPO2)  # 12.0

S_DROP = 0.30  # Drop > s => ON (DROP_EVENT)
P_FLAT = 0.01  # |vE| <= p => flat note

E_RESET = 0.5556        # reset persistence when E >= this
E_VLOW = 0.1597         # very-low threshold (<=) for 3-step persistence
E_LOW_MAX = 0.4375      # low threshold (<=) for 5-step persistence (and > E_VLOW)
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
    Parse user input as a sequence of integers.
    Accepts newline-separated, comma-separated, space-separated.
    """
    if not text.strip():
        return []
    # Split on commas, spaces, semicolons, newlines
    tokens = re.split(r"[,\s;]+", text.strip())
    vals: List[int] = []
    for tok in tokens:
        if not tok:
            continue
        # Keep only numeric tokens (allow leading +/- though spo2 should be positive)
        if not re.fullmatch(r"[+-]?\d+", tok):
            continue
        vals.append(int(tok))
        if len(vals) >= MAX_POINTS:
            break
    return vals


def clamp_spo2(x: int) -> int:
    # Guardrail (non-invasive): keep in plausible range; still show original int in UI
    return max(50, min(100, x))


def spo2_to_e(spo2: int) -> float:
    """
    E-based encoding (locked):
      T = clip((100 - spo2)/12, 0, 1)
      E = 1 - T^2
    """
    s = float(clamp_spo2(spo2))
    t = (GOOD_SPO2 - s) / DENOM
    if t < 0.0:
        t = 0.0
    elif t > 1.0:
        t = 1.0
    e = 1.0 - (t * t)
    return e


def compute_table(spo2_raw: List[int]) -> pd.DataFrame:
    """
    Produce output table:
      Minute | SpO2 | E | vE | Drop | Alert | Reason | Note
    Logic priority:
      LIMIT_E0 > DROP_EVENT > VERY_LOW_PERSIST > LOW_PERSIST > NO_TRIGGER
    Persistence counters are internal and NOT exported.
    """
    rows: List[RowOut] = []

    prev_e: Optional[float] = None
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

        # ---- Update persistence counters (E-based) ----
        # Reset rule: when reserve recovers above reset threshold
        if e >= E_RESET:
            cnt_low = 0
            cnt_vlow = 0
            note_parts.append("reset (E>=E_reset)")
        else:
            if e <= E_VLOW:
                cnt_vlow += 1
                cnt_low += 1
                # optional note (no counters shown)
                if cnt_vlow < N_VLOW:
                    note_parts.append("counting very-low persistence")
            elif (e > E_VLOW) and (e <= E_LOW_MAX):
                cnt_low += 1
                cnt_vlow = 0
                if cnt_low < N_LOW:
                    note_parts.append("counting low persistence")
            else:
                # e > E_LOW_MAX and < E_RESET -> not in low zones
                cnt_low = 0
                cnt_vlow = 0

        # ---- Determine Alert/Reason by priority ----
        alert = "OFF"
        reason = "NO_TRIGGER"

        # LIMIT_E0: reserve minimum
        if e <= 1e-12:
            alert = "ON*"
            reason = "LIMIT_E0"
        # DROP_EVENT: acute deterioration
        elif drop is not None and drop > S_DROP:
            alert = "ON"
            reason = "DROP_EVENT"
        # VERY_LOW_PERSIST
        elif cnt_vlow >= N_VLOW and e <= E_VLOW:
            alert = "ON"
            reason = "VERY_LOW_PERSIST"
        # LOW_PERSIST
        elif cnt_low >= N_LOW and (e > E_VLOW) and (e <= E_LOW_MAX):
            alert = "ON"
            reason = "LOW_PERSIST"

        # OFF note (optional clarity)
        if alert == "OFF" and "flat (|vE|<=p)" in note_parts:
            # already informative enough
            pass
        elif alert == "OFF" and reason == "NO_TRIGGER":
            # keep it simple; note already has counting/reset/first sample if relevant
            pass

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
    # Pretty rounding for display (does not change logic)
    df["e"] = df["e"].round(4)
    df["ve"] = df["ve"].round(4)
    df["drop"] = df["drop"].round(4)
    df = df.rename(
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
    return df


def main() -> None:
    st.set_page_config(page_title="DN pro • SpO2 (E-based)", layout="wide")
    st.title("DN pro • SpO₂ (E-based) — 1 step = 1 minute")

    st.caption(
        "Input raw SpO₂ (max 100 points). The app encodes each point into reserve level E, "
        "computes vE/Drop, then labels Alert (ON/OFF/ON*)."
    )

    default_series = "93\n91\n90\n89\n88\n89\n89\n91\n92\n89\n90\n91\n92\n91\n89\n90"
    raw_text = st.text_area(
        "Paste SpO₂ series (one value per line; commas/spaces also accepted).",
        value=default_series,
        height=220,
    )

    series = parse_spo2_series(raw_text)
    if not series:
        st.info("Enter SpO₂ values to compute the table.")
        return

    if len(series) > MAX_POINTS:
        series = series[:MAX_POINTS]

    st.write(f"Parsed **{len(series)}** points (each point = **1 minute**).")

    df = compute_table(series)

    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("### Notes (E-based rules)")
    st.markdown(
        f"""
- Each row represents **1 minute**. Raw values are first encoded into **reserve level E**, after which all rules are applied.
- **ON\\*** (**`LIMIT_E0`**) when the encoded reserve reaches its minimum (**E = 0**).
- **ON** (**`DROP_EVENT`**) when acute deterioration exceeds threshold (**Drop > s**, with **s = {S_DROP:.2f}**).
- **ON** (**`VERY_LOW_PERSIST`**) when **E ≤ {E_VLOW:.4f}** persists for **{N_VLOW} consecutive steps**.
- **ON** (**`LOW_PERSIST`**) when **{E_VLOW:.4f} < E ≤ {E_LOW_MAX:.4f}** persists for **{N_LOW} consecutive steps**.
- Persistence counters reset when reserve recovers (**E ≥ {E_RESET:.4f}**).
- **OFF** (**`NO_TRIGGER`**) indicates no ON condition met. `flat` denotes stable short-term dynamics (**|vE| ≤ p**, with **p = {P_FLAT:.2f}**).
"""
    )

    with st.expander("Locked parameters (read-only)"):
        st.code(
            f"""good=100, bad=88 (clip T in [0,1])
s={S_DROP:.2f}, p={P_FLAT:.2f}
E_reset={E_RESET:.4f}
E_vlow={E_VLOW:.4f} for {N_VLOW} steps
E_low_max={E_LOW_MAX:.4f} for {N_LOW} steps
max_points={MAX_POINTS}""",
            language="text",
        )


if __name__ == "__main__":
    main()
