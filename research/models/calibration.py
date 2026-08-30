from __future__ import annotations

import numpy as np


def brier_score(probabilities, outcomes) -> float:
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    return float(np.mean((p - y) ** 2)) if len(p) else float("nan")


def log_loss(probabilities, outcomes, eps: float = 1e-6) -> float:
    p = np.clip(np.asarray(probabilities, dtype=float), eps, 1 - eps)
    y = np.asarray(outcomes, dtype=float)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p))) if len(p) else float("nan")


def calibration_bins(probabilities, outcomes, bins: int = 10) -> list[dict]:
    p = np.asarray(probabilities, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    rows: list[dict] = []
    for low in np.linspace(0, 1, bins, endpoint=False):
        high = low + 1 / bins
        mask = (p >= low) & (p < high if high < 1 else p <= high)
        if mask.any():
            rows.append({"bin_low": low, "bin_high": high, "avg_pred": float(p[mask].mean()), "avg_outcome": float(y[mask].mean()), "n": int(mask.sum())})
    return rows
