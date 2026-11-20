"""Peer prediction branches and explicit consumer selection are wire invariants."""
from copy import deepcopy

import pytest
from jsonschema import ValidationError as SchemaError
from pydantic import ValidationError as ModelError

from clawtune_kb.contracts import validate
from clawtune_sidecar.contracts.models import ToolPrediction
from clawtune_sidecar.signals import SignalHub
from prediction_fixtures import prediction_example


def test_pre_execution_prediction_has_only_three_peer_models():
    value = prediction_example().model_dump(mode="json")
    assert set(value) == {"tool", "trie", "lattice"}
    assert set(value["tool"]) == {"load", "pmu"}
    assert set(value["trie"]) == set(value["lattice"]) == {"load"}
    for branch in value.values():
        assert len(branch["load"]["targets"]) == 7
    assert len(value["tool"]["pmu"]["targets"]) == 9
    validate(value, "tool-prediction.schema.json")
    assert ToolPrediction.model_validate(value).model_dump(mode="json") == value


@pytest.mark.parametrize("field", ["duration_p50_ms", "duration_p90_ms", "resource_class",
                                    "confidence", "call_prediction", "pmu_prediction",
                                    "diagnostics", "tool_resource"])
def test_removed_primary_fields_are_rejected(field):
    value = prediction_example().model_dump(mode="json")
    value[field] = None
    with pytest.raises(SchemaError):
        validate(value, "tool-prediction.schema.json")
    with pytest.raises(ModelError):
        ToolPrediction.model_validate(value)


@pytest.mark.parametrize("branch", ["tool", "trie", "lattice"])
def test_missing_or_mislabeled_model_is_not_filled_from_another(branch):
    value = prediction_example().model_dump(mode="json")
    missing = deepcopy(value)
    del missing[branch]
    with pytest.raises(SchemaError):
        validate(missing, "tool-prediction.schema.json")
    with pytest.raises(ModelError):
        ToolPrediction.model_validate(missing)
    value[branch]["load"]["targets"]["duration_ms"]["backend"] = "trie" if branch == "tool" else "runtime"
    with pytest.raises(SchemaError):
        validate(value, "tool-prediction.schema.json")
    with pytest.raises(ModelError):
        ToolPrediction.model_validate(value)


def test_signal_publishes_all_models_without_selecting_a_primary():
    from types import SimpleNamespace
    prediction = prediction_example()
    signal = SignalHub().prediction(SimpleNamespace(tool_call_id="call"), prediction, "prediction")
    assert signal["payload"]["prediction"] == prediction.model_dump(mode="json")
    validate(signal, "signal.schema.json")
