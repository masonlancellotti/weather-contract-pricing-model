from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class GradientBoostingWeatherModel:
    """Optional settlement-temperature model.

    It deliberately refuses to train on tiny samples so the system does not
    manufacture precision from inadequate history.
    """

    min_samples: int = 500
    model: object | None = None
    residual_std: float | None = None

    def fit(self, frame, target_column: str) -> "GradientBoostingWeatherModel":
        if len(frame) < self.min_samples:
            raise ValueError(f"Need at least {self.min_samples} labeled samples, got {len(frame)}.")
        from sklearn.ensemble import HistGradientBoostingRegressor

        x = frame.drop(columns=[target_column])
        y = frame[target_column]
        self.model = HistGradientBoostingRegressor(max_iter=200, learning_rate=0.05, l2_regularization=0.05)
        self.model.fit(x, y)
        preds = self.model.predict(x)
        self.residual_std = float(np.std(y - preds))
        return self

    def predict_temperature(self, frame) -> np.ndarray:
        if self.model is None:
            raise RuntimeError("Model is not trained.")
        return self.model.predict(frame)
