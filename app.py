# app_spo2.py
# DN pro • SpO2 (E-based) — 1 step = 1 minute
# CONFIG: bad=85, limit=85 (E=0 at/below 85)
#
# Rules (as in your screenshot, with limit=85):
# I) LIMIT (E=0):
#   - ON* for 5 minutes, then OFF even if still at limit
#   - OFF early if strong recovery (e.g., 85 -> 92/93) => E >= E_RESET
#   - After OFF, if still in very-low band (E <= E_VERY_LOW), do NOT ON immediately
#     -> re-notify after 10 minutes if still very-low/limit (reminder)
#
# II) DROP_EVENT:
#   - ON for 3 minutes when Drop > s
#   - OFF early if recovery strong (E >= E_RESET)
#   - After OFF: if only small oscillations and no new Drop > s -> OFF forever
#   - ON again only if new DROP_EVENT or fall back to LIMIT
#
# Input: comma-separated SpO2 series, max 100 points. Button to generate output.
#
# Requirements: streamlit, pandas

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd
import streamlit as st

# -------------------------
# Locked mapping: bad=85
# -------------------------
GOOD = 100.0
BAD = 85.0
DEN = GOOD - BAD  # 15

EPS = 1e-9
MAX_POINTS = 100

# E thresholds derived from the mapping (rounded for display, compare with EPS)
# E(92) with bad=85: T=8/15, E=1-(8/15)^2 = 1 - 64/225 = 161/225 = 0.715555...
E_RESET = 161.0 / 225.0  # ~0.7156

# "very-low band" in the screenshot was ~E 0..0.16; keep as E-based gate
E_VERY_LOW = 0.16

# Drop threshold s chosen to preserve: 92->89 triggers, 92->90 and 92->91 do not
# With bad=85:
#   Drop(92->89) ~ 0.7156 - 0.4622 = 0.2534
#   Drop(92->90) ~ 0.7156 - 0.5556 = 0.1600
#   Drop(92->91) ~ 0.7156 - 0.6400 = 0.0756
S_DROP = 0.20

# Flat note threshold (only for Note)
P_FLAT = 0.01

# Windows (minutes == steps)
LIMIT_ON_MIN = 5
LIMIT_REMINDER_MIN = 10
DROP_ON_MIN = 3


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
    """
    T = clip((100 - SpO2)/15, 0, 1)
    E = 1 - T^2
    """
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

    # Timers / states
    limit_on_left = 0          # remaining ON* minutes (limit window)
    limit_cooldown_left = 0    # remaining OFF minutes before reminder can fire (if very-low persists)

    drop_on_left = 0           # remaining ON minutes for DROP_EVENT window

    # We need edge detection for entering LIMIT
    prev_in_limit = False

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

        in_limit = (e <= 0.0 + EPS)  # E=0 means SpO2<=85 under bad=85

        # -------------------------
        # OFF early on strong recovery (example: 85 -> 92/93)
        # Only meaningful if we are recovering upward strongly
        # -------------------------
        if e >= E_RESET - EPS:
            if limit_on_left > 0 or limit_cooldown_left > 0 or drop_on_left > 0:
                note_parts.append("OFF early: strong recovery (E>=E_reset)")
            limit_on_left = 0
            limit_cooldown_left = 0
            drop_on_left = 0

        # -------------------------
        # LIMIT entry trigger (edge only): prev not in limit, now in limit
        # -------------------------
        if (not prev_in_limit) and in_limit:
            # Start LIMIT ON window (5 min) and arm reminder cooldown (10 min) after it ends
            limit_on_left = LIMIT_ON_MIN
            # cooldown starts AFTER ON finishes, but we can pre-load it here and only count down later
            limit_cooldown_left = LIMIT_REMINDER_MIN
            note_parts.append("LIMIT entry: start ON* (5 min)")

        # -------------------------
        # DROP_EVENT trigger (independent): Drop > s
        # -------------------------
        if (drop is not None) and (drop > S_DROP):
            drop_on_left = DROP_ON_MIN
            note_parts.append("DROP_EVENT: start ON (3 min)")

        # -------------------------
        # LIMIT reminder logic:
        # After LIMIT ON finishes, if still very-low/limit, wait 10 min then ON* again 5 min.
        # We implement as:
        #  - while limit_on_left>0: do not count down cooldown
        #  - when limit_on_left==0 and still (e<=E_VERY_LOW or in_limit): count down cooldown
        #  - when cooldown reaches 0 and still very-low/limit: start ON* window again and reload cooldown
        # -------------------------
        if limit_on_left == 0:
            if (in_limit or e <= E_VERY_LOW + EPS):
                if limit_cooldown_left > 0:
                    # countdown happens in OFF phase
                    limit_cooldown_left -= 1
                else:
                    # fire reminder
                    limit_on_left = LIMIT_ON_MIN
                    limit_cooldown_left = LIMIT_REMINDER_MIN
                    note_parts.append("LIMIT reminder: ON* again (5 min)")
            else:
                # if not in very-low, disarm reminder
                limit_cooldown_left = 0

        # -------------------------
        # Decide Alert/Reason for this minute
        # Priority: LIMIT_ON > DROP_EVENT > OFF
        # -------------------------
        alert = "OFF"
        reason = "NO_TRIGGER"

        if limit_on_left > 0:
            alert = "ON*"
            reason = "LIMIT_ON"
        elif drop_on_left > 0:
            alert = "ON"
            reason = "DROP_EVENT"

        # -------------------------
        # Decrement ON windows AFTER emitting
        # -------------------------
        if limit_on_left > 0:
            limit_on_left -= 1

        if drop_on_left > 0:
            drop_on_left -= 1

        rows.append(
            RowOut(
                minute=i,
                spo2=int(raw),
                e=round(e, 4),
                ve=None if ve is None else round(ve, 4),
                drop=None if drop is None else round(drop, 4),
                alert=alert,
                reason=reason,
                note="; ".join(note_parts),
            )
        )

        prev_e = e
        prev_in_limit = in_limit

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

    st.caption("Input raw SpO₂ as comma-separated values (max 100). Click the button to generate output.")

    default_line = "92,91,90,89,85,85,85,90,91,92,89,90,91,92"
    text = st.text_input("SpO₂ series (comma-separated):", value=default_line)

    col1, col2 = st.columns([1, 3])
    with col1:
        run = st.button("Generate output", type="primary")
    with col2:
        st.markdown("**Each point = 1 minute.** Max **100** values.")

    if not run:
        st.info("Enter SpO₂ series above, then click **Generate output**.")
        return

    series = parse_series_csv(text)
    if not series:
        st.error("No valid numbers found. Example: 92,91,90,89,85,85,90,92")
        return

    if len(series) > MAX_POINTS:
        series = series[:MAX_POINTS]
        st.warning(f"Input exceeded {MAX_POINTS} points; only first {MAX_POINTS} used.")

    df = compute_table(series)
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.markdown("---")
    st.markdown("## Hướng dẫn dùng (đúng quy ước như ảnh, limit=85)")
    st.markdown(
        f"""
### I. ON do LIMIT (E = 0, ví dụ SpO₂ = **85**)
1) **Điều kiện ON:** khi **E = 0** → ON (LIMIT)  
2) **ON trong bao lâu:** ON đúng **5 phút** (= 5 cửa sổ, mỗi cửa sổ 1 phút)  
3) **OFF khi nào:** OFF sau đủ **5 phút**, kể cả vẫn còn ở limit  
4) **OFF sớm khi nào:** OFF sớm nếu có hồi phục mạnh (ví dụ **85 → 92/93**) (**E ≥ {E_RESET:.4f}**)  
5) **ON lại nếu vẫn “lắc” ở very-low:** không ON lại ngay  
6) **Reminder:** ON lại sau **10 phút** nếu vẫn ở very-low/limit

➡️ **Chốt LIMIT:** ON 5 phút → OFF → nếu vẫn very-low thì 10 phút sau ON lại

---

### II. ON do DROP_EVENT (Drop > s)
1) **Điều kiện ON:** chỉ khi **Drop > s** (s = **{S_DROP:.2f}**)  
2) **ON trong bao lâu:** ON đúng **3 phút**  
3) **OFF khi nào:** OFF sau đủ **3 phút**  
4) **OFF sớm khi nào:** OFF sớm nếu có hồi phục rõ (E ≥ {E_RESET:.4f})  
5) **Sau khi OFF:** nếu chỉ dao động nhỏ và không có Drop > s mới → OFF vĩnh viễn  
6) **ON lại khi nào:** chỉ khi có DROP_EVENT mới hoặc rơi lại LIMIT

➡️ **Chốt DROP_EVENT:** ON 3 phút → OFF → không ON lại nếu không có drop mới
"""
    )


if __name__ == "__main__":
    main()
