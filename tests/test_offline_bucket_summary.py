"""Offline summaries exclude non-shell calls and retain shell abstentions."""
import json

import pytest

from tools.summarize_offline_buckets import compare, summarize


def test_bucket_recall_counts_abstention_and_reports_no_prediction_precision():
    rows = [
        {"actual_bucket": 0, "predicted_bucket": 0},
        {"actual_bucket": 0, "predicted_bucket": None},
        {"actual_bucket": 1, "predicted_bucket": 0},
        {"actual_bucket": 1, "predicted_bucket": 1},
        {"actual_bucket": 2, "predicted_bucket": None},
    ]
    result = summarize(rows, 3)
    assert result["accuracy_available"] == pytest.approx(2 / 3)
    assert result["micro_recall_all"] == pytest.approx(2 / 5)
    assert result["macro_recall_all"] == pytest.approx(1 / 3)
    assert result["coverage"] == pytest.approx(3 / 5)
    assert result["per_bucket"][0]["precision"] == .5
    assert result["per_bucket"][0]["recall_available"] == 1
    assert result["per_bucket"][0]["recall_all"] == .5
    assert result["per_bucket"][2]["precision"] is None
    assert result["per_bucket"][2]["recall_all"] == 0
    assert result["confusion_matrix_with_abstention_last"] == [[1, 0, 0, 1], [1, 1, 0, 0], [0, 0, 0, 1]]


@pytest.mark.parametrize("shell_tool", ["exec", "terminal_exec", None])
def test_compare_old_mixed_export_scores_only_shell_calls(tmp_path, shell_tool):
    rows, model_metrics = [], {}
    for model in ("tool", "trie", "lattice"):
        base = dict(model=model, task="test", benchmark="bfcl", repo="dataset", target="duration_ms")
        selected = [{**base, "tool": "read_file", "actual": 1, "actual_bucket": 0,
                     "predicted_bucket": 0 if model == "tool" else None}]
        if shell_tool:
            selected.append({**base, "tool": shell_tool, "actual": 150, "actual_bucket": 1,
                             "predicted_bucket": 0 if model == "tool" else 1})
        raw = summarize(selected, 2)
        model_metrics[model] = {"metrics": [{"target": "duration_ms", "bucket_metrics": {
            "eligible": raw["eligible"], "predicted": raw["predicted"],
            "accuracy": raw["accuracy_available"]}}]}
        rows.extend(selected)
    report = dict(train_tasks=1, test_tasks=1, test_updates=0, split_sha256="fixed",
                  bucket_edges={"duration_ms": [100]}, models=model_metrics)
    split = dict(seed=42, train_fraction=.8, tasks={"train": {"files": [{}]}, "test": {"files": [{}]}})
    (tmp_path / "report.json").write_text(json.dumps(report))
    (tmp_path / "split.json").write_text(json.dumps(split))
    (tmp_path / "model-predictions.jsonl").write_text("\n".join(map(json.dumps, rows)))
    if shell_tool is None:
        with pytest.raises(ValueError, match="no shell duration predictions"):
            compare(tmp_path)
        return
    result = compare(tmp_path)
    assert result["scope"] == "shell_calls"
    assert result["shell_eligible"] == result["common_count"] == 1
    assert result["models"]["tool"]["accuracy_available"] == 0
    assert result["models"]["trie"]["accuracy_available"] == 1
    assert all(model["eligible"] == 1 for model in result["models"].values())
    assert result["models"]["tool"]["per_bucket"][0]["support_all"] == 0
    assert result["models"] == result["shell_models"]
