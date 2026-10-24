import json
from pathlib import Path

from clawtune_kb import default_seed_directory, validate_seed
from tool_resource.runtime_kb import RuntimeToolResourceKB
from tool_time.lattice_kb import LatticeTimeKB


def test_qualified_prior_is_separate_and_loadable_by_admission_models():
    root = Path(__file__).resolve().parents[1]
    assert default_seed_directory() == root / "seeds/bootstrap-v2"
    qualified = root / "seeds/clawbox-qualified-v1"
    manifest = validate_seed(qualified)
    provenance = manifest["provenance"]
    assert provenance["kind"] == "clawbox-qualified-observation-import"
    assert provenance["admission"]["sandbox_identities"]
    assert provenance["admission"]["host_calibrations"]
    assert provenance["admission_prediction_policy"]
    assert validate_seed(root / "seeds/bootstrap-v2")["provenance"]["kind"] == "repository-all-trace-v6"
    lattice = LatticeTimeKB.from_json_obj(json.loads((qualified / "clause-lattice-time-kb.json").read_text()))
    tool = RuntimeToolResourceKB.from_json_obj(json.loads((qualified / "runtime-tool-resource-kb.json").read_text()))
    lattice.freeze()
    tool.freeze()
