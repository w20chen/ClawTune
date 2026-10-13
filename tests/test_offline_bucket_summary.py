"""Abstentions must affect all-label recall but not conditional accuracy."""
import pytest

from tools.summarize_offline_buckets import summarize


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
