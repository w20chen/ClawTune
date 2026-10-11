"""Run under either checkout's PYTHONPATH to compare unchanged KB mathematics."""
import inspect
import json
from dataclasses import asdict

from edge_kappa_kb import EdgeKappaKB, FeatureQuery, TimeOutcome, TrainingEvent
from tool_time._lattice_vendor import nodes, normalize, selector
from tool_time._lattice_vendor.schemas import Observation
from tool_time._lattice_vendor.shrinkage import compute_shrinkage_variances

CORE = frozenset({"tool=fixture"})
FEATURES = {name: CORE | set(name) for name in ("a", "ab", "ac", "bc", "abc")}


def fixed(command, **kwargs):
    return FEATURES[command], CORE


nodes.normalize_command = fixed
normalize.normalize_command = fixed
selector.normalize_command = fixed
data = [("a", .1), ("a", .3), ("ab", 2.), ("ac", 5.), ("bc", 10.)]
records = []
for command, duration in data:
    kwargs = {}
    if "command_features" in inspect.signature(Observation).parameters:
        from command_parser.models import CommandFeatures
        kwargs["command_features"] = CommandFeatures("fixture", FEATURES[command], CORE, "0" * 64).to_dict()
    records.append(Observation(cmd=command, duration_s=duration, **kwargs))
built, variance, std = nodes.build_nodes(records, split_compounds=False, max_optional_features=2, subset_coverage=True)
compute_shrinkage_variances(built, global_log_var=variance)
predictions = []
for command, features in FEATURES.items():
    for algorithm in ("shrinkage", "loso"):
        kwargs = {"query_features": features} if "query_features" in inspect.signature(selector.predict).parameters else {}
        predictions.append(asdict(selector.predict(command, built, global_log_var=variance,
                                                   global_log_std=std, risk_method=algorithm, **kwargs)))
events = [TrainingEvent(FeatureQuery(FEATURES[command], CORE, "fixture", "fixed-inputs"),
          TimeOutcome(str(i), float(i), i + .1, "task", str(i), "0", duration_ms=duration * 1000))
          for i, (command, duration) in enumerate(data)]
edge = EdgeKappaKB.fit(events, {"normalization_version": "fixed-inputs"})
output = {"lattice_nodes": [asdict(built[key]) for key in sorted(built, key=lambda f: sorted(f))],
          "lattice_predictions": predictions, "edge_snapshot": edge.to_snapshot(),
          "edge_predictions": [asdict(edge.predict_duration(FeatureQuery(fs, CORE, "fixture", "fixed-inputs")))
                               for fs in FEATURES.values()]}
print(json.dumps(output, sort_keys=True, default=lambda value: sorted(value), allow_nan=False))
