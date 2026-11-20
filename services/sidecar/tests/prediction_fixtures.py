"""Canonical three-model prediction fixture; no implicit primary model."""
import json
from pathlib import Path

from clawtune_sidecar.contracts.models import ToolPrediction


def prediction_example():
    path = Path(__file__).resolve().parents[3] / "contracts/examples/tool-prediction.json"
    return ToolPrediction.model_validate(json.loads(path.read_text(encoding="utf-8")))
