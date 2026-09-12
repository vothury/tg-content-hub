"""Подготовка рядов для SVG-баров (без внешних библиотек)."""
from __future__ import annotations


def bar_series(pairs, height: int = 110):
    """pairs: list[(label, value)] -> (series, max). series[i] = {label, value, h}."""
    vals = [v for _, v in pairs]
    mx = max(vals, default=0) or 1
    out = []
    for label, v in pairs:
        h = round(v / mx * (height - 18), 1)
        out.append({"label": label, "value": v, "h": max(2.0, h) if v > 0 else 0.0})
    return out, mx


def stacked_series(triples, height: int = 110):
    """triples: list[(label, v_base, v_top)] -> (series, max)."""
    mx = max((a + b) for _, a, b in triples) or 1
    out = []
    for label, a, b in triples:
        h1 = round(a / mx * (height - 18), 1)
        h2 = round(b / mx * (height - 18), 1)
        out.append({"label": label, "v1": a, "v2": b, "h1": h1, "h2": h2})
    return out, mx