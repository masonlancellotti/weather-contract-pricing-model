from __future__ import annotations

from models.baseline_model import HeuristicWeatherModel


class ModelRegistry:
    def __init__(self):
        self._models = {"heuristic": HeuristicWeatherModel()}

    def get(self, name: str = "heuristic"):
        if name not in self._models:
            raise KeyError(f"Unknown model {name}. Available: {sorted(self._models)}")
        return self._models[name]
