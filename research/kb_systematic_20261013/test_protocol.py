import math
from research.kb_systematic_20261013.study import metrics


def test_missing_predictions_are_not_zero_or_correct():
    rows=[dict(actual_ms=0.,actual_bucket=0,p50=0.,predicted_bucket=0),
          dict(actual_ms=100.,actual_bucket=1,p50=None,predicted_bucket=None),
          dict(actual_ms=10000.,actual_bucket=4,p50=1.,predicted_bucket=0)]
    m=metrics(rows)
    assert m['coverage']==2/3 and m['accuracy']==.5 and m['accuracy_all']==1/3
    assert m['per_bin'][1]['support']==1 and m['per_bin'][1]['recall_all']==0
    assert m['per_bin'][1]['recall_covered'] is None
    assert m['factor2']==.5 and m['severe_error']==.5
    assert math.isclose(m['log_mae'],(math.log1p(10)-math.log1p(.001))/2)


def test_empty_scope_has_no_invented_error():
    m=metrics([])
    assert m['n']==0 and m['mae_s'] is None and m['coverage'] is None


def test_bucket_accuracy_uses_distribution_mode_not_median_bucket():
    row=dict(actual_ms=200.,actual_bucket=1,p50=600.,predicted_bucket=1)
    assert metrics([row])['accuracy']==1
    assert metrics([row])['factor2']==0
