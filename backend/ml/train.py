from __future__ import annotations

import pickle
from pathlib import Path

from sklearn.ensemble import RandomForestRegressor


MODEL_PATH = Path("/app/models/ai_satei_model.pkl")


def main() -> None:
    features = [
        [50, 1, 0],
        [65, 1, 1],
        [80, 2, 1],
        [95, 3, 1],
        [120, 4, 2],
        [150, 5, 2],
    ]
    targets = [900, 1100, 1500, 2100, 2800, 3600]

    model = RandomForestRegressor(n_estimators=20, random_state=42)
    model.fit(features, targets)

    MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with MODEL_PATH.open("wb") as file:
        pickle.dump(model, file)

    print(f"model trained: {MODEL_PATH}")


if __name__ == "__main__":
    main()
