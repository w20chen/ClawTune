from dataclasses import asdict
import hashlib
import json

import pytest

from clawtune_kb import FILES, create_seed, validate_seed
from clawtune_sidecar.predictors.edge_kappa import EdgeKappaRuntime
from scripts.build_clawbox_seed import build
from tool_resource.runtime_kb import (
    ClauseObservation,
    ClauseResourceKB,
    CompletedCall,
    RuntimeToolResourceKB,
)
from tool_time.lattice_kb import LatticeTimeKB


MEASUREMENT = "guest_memtotal_minus_memavailable"


def _base_seed(path):
    payloads = dict(zip(FILES, (
        ClauseResourceKB().to_json_obj(),
        RuntimeToolResourceKB().to_json_obj(),
        LatticeTimeKB().to_json_obj(),
    )))
    payloads["edge-kappa-kb.json"] = EdgeKappaRuntime.fit(
        [], (100, 500, 2000, 10000)
    ).to_snapshot()
    return create_seed(path, payloads, provenance={"kind": "test-base"})


def _bundle(path):
    call = CompletedCall(
        repo="owner/repo", tool_name="exec", command="python work.py",
        ts_start=1, ts_end=2,
        memory_baseline_bytes=100, memory_total_peak_bytes=300,
        memory_extra_peak_bytes=200, memory_measurement=MEASUREMENT,
        memory_environment_id="vm-1", memory_eligible=True,
        sampled_peak_rss_bytes=400, sampled_peak_rss_eligible=True,
    )
    clause = ClauseObservation(
        repo="owner/repo", bin="python", argv=("python", "work.py"),
        ts_start=1.1, ts_end=1.9, latency_ms=800,
        memory_baseline_bytes=100, memory_total_peak_bytes=300,
        memory_extra_peak_bytes=200, memory_measurement=MEASUREMENT,
        memory_environment_id="vm-1", memory_eligible=True,
        sampled_peak_rss_mb=0.001,
    )
    execution_id = "execution-1"
    payload = {
        "schema": "clawtune.seed_observations.v1",
        "producer": {
            "name": "ClawBox", "clawtune_revision": "a" * 40,
            "exporter_sha256": "b" * 64,
        },
        "validation": {
            "environment_calibration_result": "/result.json",
            "environment_calibration_result_sha256": "c" * 64,
            "qualification_receipt": "/qualification.json",
            "qualification_receipt_sha256": "d" * 64,
            "qualification_cleanup_verified": True,
            "covered_case_count": 1,
            "paired_command_count": 1,
            "paired_session_count": 1,
            "maintenance_measurement_count": 1,
            "public_prior_scope": "cross_repository_tool_class",
            "public_prior_completed_call_count": 1,
            "public_prior_memory_label_count": 1,
            "lattice_clause_observation_count": 1,
            "lattice_memory_label_count": 1,
            "public_prior_repository_count": 1,
            "public_prior_execution_ids_sha256": hashlib.sha256(
                execution_id.encode()
            ).hexdigest(),
        },
        "admission": {
            "sandbox_identities": [{
                "template_id": "template", "image_digest": "sha256:image",
                "vcpu": 2, "memory_mib": 2048, "architecture": "arm64",
            }],
            "sandbox_identities_sha256": "e" * 64,
            "host_calibration": {
                "target": "host_cgroup_memory_current_increment",
                "method": "heterogeneous_environment_p90_ratio_plus_p90_residual",
                "sample_count": 1, "scale": 1, "offset_bytes": 0,
            },
            "non_command_tool_memory_calibration": {
                "target": "host_cgroup_memory_current_increment",
                "sample_count": 1, "p90_bytes": 1, "p99_bytes": 1,
                "recommended_mib": 1,
            },
        },
        "observations": {
            "completed_calls": [{
                "execution_id": execution_id,
                "host_cgroup_execution_increment_bytes": 250,
                "call": asdict(call),
            }],
            "clauses": [{
                "execution_id": execution_id, "clause_index": 0,
                "observation": asdict(clause),
            }],
        },
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def test_builds_standard_seed_with_lattice_and_public_tool_memory(tmp_path):
    base = tmp_path / "bootstrap-v1"
    _base_seed(base)
    observations = tmp_path / "observations.json"
    _bundle(observations)

    manifest = build(tmp_path / "bootstrap-v2", observations, base)

    assert validate_seed(tmp_path / "bootstrap-v2") == manifest
    provenance = manifest["provenance"]
    assert provenance["kind"] == "clawbox-qualified-observation-import"
    assert provenance["recipe_version"] == 4
    assert provenance["coverage"]["added_lattice_observations"] == 1
    assert provenance["retained_base_optional_snapshots"] == [
        "edge-kappa-kb.json"
    ]
    assert provenance["coverage"]["tool_memory_labels_by_measurement"][MEASUREMENT][
        "memory_extra_peak_bytes"
    ] == 1
    assert provenance["admission"]["host_calibrations"][
        "lattice_sampled_peak_rss_bytes"
    ]["sample_count"] == 1
    assert [row["semantics"] for row in provenance["admission_prediction_policy"]] == [
        "exact", "proxy", "exact", "exact",
    ]
    runtime = RuntimeToolResourceKB.from_json_obj(json.loads(
        (tmp_path / "bootstrap-v2" / FILES[1]).read_text(encoding="utf-8")
    ))
    assert any(runtime.to_json_obj()["public"].values())
    lattice = LatticeTimeKB.from_json_obj(json.loads(
        (tmp_path / "bootstrap-v2" / FILES[2]).read_text(encoding="utf-8")
    ))
    assert lattice.observation_count == 1
    assert "edge-kappa-kb.json" in manifest["snapshots"]
    assert (
        tmp_path / "bootstrap-v2" / "edge-kappa-kb.json"
    ).read_bytes() == (base / "edge-kappa-kb.json").read_bytes()


def test_rejects_duplicate_execution_ids_before_creating_seed(tmp_path):
    base = tmp_path / "bootstrap-v1"
    _base_seed(base)
    observations = tmp_path / "observations.json"
    payload = _bundle(observations)
    payload["observations"]["completed_calls"].append(
        payload["observations"]["completed_calls"][0]
    )
    payload["validation"]["public_prior_completed_call_count"] = 2
    observations.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="duplicate completed-call execution_id"):
        build(tmp_path / "bootstrap-v2", observations, base)
    assert not (tmp_path / "bootstrap-v2").exists()


@pytest.mark.parametrize("missing", ["rss", "host"])
def test_guest_memory_seed_does_not_require_paired_rss(tmp_path, missing):
    base = tmp_path / "base"
    _base_seed(base)
    observations = tmp_path / "observations.json"
    payload = _bundle(observations)
    if missing == "rss":
        payload["observations"]["clauses"][0]["observation"]["sampled_peak_rss_mb"] = None
    else:
        payload["observations"]["completed_calls"][0]["host_cgroup_execution_increment_bytes"] = None
    observations.write_text(json.dumps(payload), encoding="utf-8")

    manifest = build(tmp_path / "seed", observations, base)

    assert validate_seed(tmp_path / "seed") == manifest
    provenance = manifest["provenance"]
    assert set(provenance["admission"]["host_calibrations"]) == {"guest_memory_extra_peak_bytes"}
    assert all(row["semantics"] == "exact" for row in provenance["admission_prediction_policy"])
    assert provenance["coverage"]["added_lattice_observations"] == 1


def test_schema_extensions_do_not_break_portable_observation_import(tmp_path):
    base = tmp_path / "base"
    _base_seed(base)
    path = tmp_path / "observations.json"
    payload = _bundle(path)
    payload["observations"]["completed_calls"][0]["call"]["producer_metadata"] = {"version": 2}
    payload["observations"]["clauses"][0]["observation"]["producer_metadata"] = ["additional evidence"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = build(tmp_path / "seed", path, base)
    assert manifest["provenance"]["coverage"]["completed_calls"] == 1
    assert manifest["provenance"]["coverage"]["added_lattice_observations"] == 1


@pytest.mark.parametrize("record", ["completed_calls", "clauses"])
@pytest.mark.parametrize("defect", ["missing_baseline", "missing_environment", "inconsistent_extra"])
def test_memory_qualification_matches_usable_training_labels(tmp_path, record, defect):
    base = tmp_path / "base"
    _base_seed(base)
    path = tmp_path / "observations.json"
    payload = _bundle(path)
    row = payload["observations"][record][0]["call" if record == "completed_calls" else "observation"]
    if defect == "missing_baseline":
        row.pop("memory_baseline_bytes")
    elif defect == "missing_environment":
        row.pop("memory_environment_id")
    else:
        row["memory_extra_peak_bytes"] = 999
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        build(tmp_path / "seed", path, base)
    assert not (tmp_path / "seed").exists()


def test_rejects_memory_coverage_claim_inconsistent_with_labels(tmp_path):
    base = tmp_path / "base"
    _base_seed(base)
    path = tmp_path / "observations.json"
    payload = _bundle(path)
    payload["validation"]["public_prior_memory_label_count"] = 2
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="label counts"):
        build(tmp_path / "seed", path, base)


def test_partial_call_preserves_measured_guest_memory_labels(tmp_path):
    base = tmp_path / "base"
    _base_seed(base)
    path = tmp_path / "observations.json"
    payload = _bundle(path)
    payload["observations"]["completed_calls"][0]["call"]["censored"] = True
    path.write_text(json.dumps(payload), encoding="utf-8")
    manifest = build(tmp_path / "seed", path, base)
    assert manifest["provenance"]["coverage"]["completed_calls"] == 1


def test_zero_imputation_cannot_qualify_missing_guest_measurements(tmp_path):
    base = tmp_path / "base"
    _base_seed(base)
    path = tmp_path / "observations.json"
    payload = _bundle(path)
    call = payload["observations"]["completed_calls"][0]["call"]
    call.update(memory_eligible=False, memory_baseline_bytes=None,
                memory_total_peak_bytes=None, memory_extra_peak_bytes=None)
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError, match="must contain guest extra-memory"):
        build(tmp_path / "seed", path, base)
