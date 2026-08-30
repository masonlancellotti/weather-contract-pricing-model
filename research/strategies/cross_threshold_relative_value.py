from __future__ import annotations

from strategies.ladder_consistency import LadderConsistencyStrategy


class CrossThresholdRelativeValueStrategy(LadderConsistencyStrategy):
    """Alias of ladder consistency for now; extend with model-implied RV later."""

    name = "cross_threshold_relative_value"
