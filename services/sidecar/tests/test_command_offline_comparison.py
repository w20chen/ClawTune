import json

from scripts.command_rules.compare_offline import compare


def test_offline_comparison_separates_coverage_from_paired_error(tmp_path):
    for name, predictions in (("before", [2., None]), ("after", [3., 100.])):
        folder = tmp_path / name
        folder.mkdir()
        (folder / "report.json").write_text(json.dumps({"split_sha256": "same", "train_tasks": 2,
            "test_tasks": 1, "test_updates": 0}))
        (folder / "split.json").write_text("{}")
        rows = [{"model": "lattice", "task": "task", "target": "duration_ms", "actual": 1.,
                 "p50": value, "predicted_bucket": 0, "actual_bucket": 0} for value in predictions]
        (folder / "model-predictions.jsonl").write_text("\n".join(map(json.dumps, rows)))
    result = compare(tmp_path / "before", tmp_path / "after")["comparisons"][0]
    assert result["paired"] == 1
    assert result["main"]["available"] == 2
    assert result["phase1"]["mae"] == 1.
    assert result["main"]["mae"] == 2.
