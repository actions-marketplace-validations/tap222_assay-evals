"""Human-readable values, shared by alert messages and measure notes."""
from __future__ import annotations

from typing import Optional


def fmt(v: Optional[float], unit: str) -> str:
    if v is None:
        return "—"
    if unit == "ratio":
        return f"{v * 100:.2f}%" if 0 < abs(v) < 0.01 else f"{v * 100:.1f}%"
    if unit == "seconds":
        return f"{v / 60:.0f} min" if v < 3600 else f"{v / 3600:.1f} h" if v < 172800 else f"{v / 86400:.1f} d"
    if unit == "ms":
        return f"{v:,.0f} ms" if v < 10000 else f"{v / 1000:.1f} s"
    if unit == "usd":
        return f"${v:,.2f}"
    if unit == "count":
        return f"{v:,.0f}"
    return f"{v:.3f}"
