"""Risk computation for context nodes.

For each activated node C, computes:

    R_C = L_C + beta * U_C + gamma * D_C

where:
- L_C: historical leave-one-out prediction error (log-space)
- U_C: statistical uncertainty (standard error of log-duration)
- D_C: drift risk from time decay or environmental mismatch
"""

from __future__ import annotations

import math
from datetime import datetime, timezone

from tool_time._lattice_vendor.schemas import CandidateNode, NodeStats
from tool_time._lattice_vendor.normalize import FeatureSet


def compute_candidate_risk(
    stats: NodeStats,
    query_features: FeatureSet,
    *,
    global_log_var: float,
    global_log_std: float,
    beta: float = 0.5,
    gamma: float = 0.3,
    exact_singleton_risk: float = 0.03,
    risk_method: str = "context_variance",
    context_shrinkage_m: float = 3.0,
    context_sample_alpha: float = 0.03,
    drift_enabled: bool = False,
    drift_horizon_days: float = 90.0,
    node_last_updated: str = "",
    query_timestamp: str = "",
    shrinkage_kappa: float = 5.0,
    loso_min_signatures: int = 3,
) -> CandidateNode:
    """Compute risk for an activated candidate node.

    Args:
        stats: Node statistics.
        query_features: The full query feature set.
        global_log_var: Global log-duration variance (for single-sample nodes).
        global_log_std: Global log-duration standard deviation.
        beta: Weight for uncertainty term.
        gamma: Weight for drift term.
        exact_singleton_risk: Risk for exact-match single-sample nodes.
        risk_method: ``"legacy"``, ``"context_variance"``, ``"shrinkage"``, or ``"loso"``.
        context_shrinkage_m: Strength for shrinking node variance to global variance.
        context_sample_alpha: Small-sample penalty for context-variance risk.
        drift_enabled: Whether to compute time-based drift.
        drift_horizon_days: Days after which drift saturates at 1.0.
        node_last_updated: ISO timestamp of node's last update.
        query_timestamp: ISO timestamp of the query.
        shrinkage_kappa: Shrinkage strength for ``"shrinkage"`` method (default 5).
        loso_min_signatures: Minimum distinct signatures for ``"loso"`` method
            (default 2).  Nodes with fewer signatures are excluded from selection.

    Returns:
        A CandidateNode with computed risk components.
    """
    exact = (stats.features == query_features)

    if risk_method == "loso":
        # ── Leave-One-Signature-Out risk ─────────────────────────────
        # Risk is the pre-computed LOSO prediction error:
        #   R_C = mean_{q} (z_q - z_hat_{C,-q})^2
        # where z_q = log(1 + median(durations for signature q)).
        # No uncertainty or drift terms — the LOSO MSE already captures
        # small-sample instability naturally.
        error_term = stats.loso_risk if stats.loso_risk > 0 else stats.loo_mse_log
        uncertainty_term = 0.0
        drift_term = 0.0
        risk = error_term
        return CandidateNode(
            stats=stats,
            risk=risk,
            exact=exact,
            error_term=error_term,
            uncertainty_term=uncertainty_term,
            drift_term=drift_term,
        )

    if risk_method == "shrinkage":
        # ── Bayesian shrinkage risk ──────────────────────────────────
        drift_term = _compute_drift(
            node_last_updated, query_timestamp,
            drift_enabled=drift_enabled,
            drift_horizon_days=drift_horizon_days,
        )
        # Use pre-computed shrinkage variance from NodeStats
        if stats.shrinkage_var > 0:
            error_term = stats.shrinkage_var
        elif stats.count >= 2:
            error_term = stats.std_log ** 2
        else:
            error_term = global_log_var

        # Cold-start penalty: mild extra uncertainty for top-level single-sample nodes
        if stats.cold_start:
            uncertainty_term = context_sample_alpha * 2.0  # doubled penalty
        else:
            uncertainty_term = context_sample_alpha / math.sqrt(max(stats.count, 1))

        risk = error_term + uncertainty_term + gamma * drift_term
        return CandidateNode(
            stats=stats,
            risk=risk,
            exact=exact,
            error_term=error_term,
            uncertainty_term=uncertainty_term,
            drift_term=drift_term,
        )

    if risk_method == "context_variance":
        drift_term = _compute_drift(
            node_last_updated, query_timestamp,
            drift_enabled=drift_enabled,
            drift_horizon_days=drift_horizon_days,
        )
        k = max(stats.signature_count, 1)
        m = max(context_shrinkage_m, 0.0)
        node_tau2 = max(stats.signature_log_mean_var, 0.0)
        parent_tau2 = max(global_log_var, 0.0)
        if exact:
            shrunk_tau2 = node_tau2
        elif k + m > 0:
            shrunk_tau2 = (k * node_tau2 + m * parent_tau2) / (k + m)
        else:
            shrunk_tau2 = node_tau2
        effective_count = stats.count if exact else k
        uncertainty_term = context_sample_alpha / math.sqrt(max(effective_count, 1))
        error_term = shrunk_tau2
        risk = error_term + uncertainty_term + gamma * drift_term
        return CandidateNode(
            stats=stats,
            risk=risk,
            exact=exact,
            error_term=error_term,
            uncertainty_term=uncertainty_term,
            drift_term=drift_term,
        )

    # ── Exact singleton: special low risk ─────────────────────────────
    if exact and stats.count == 1:
        error_term = 0.0
        uncertainty_term = exact_singleton_risk
        drift_term = 0.0
        risk = exact_singleton_risk
    else:
        # ── Error term (historical LOO loss) ──────────────────────────
        if stats.count >= 2:
            error_term = stats.loo_mse_log
        else:
            # Single-sample partial node: use global variance as prior
            error_term = global_log_var

        # ── Uncertainty term ──────────────────────────────────────────
        if stats.count >= 2:
            uncertainty_term = stats.stderr_log
        else:
            uncertainty_term = global_log_std

        # ── Drift term ────────────────────────────────────────────────
        drift_term = _compute_drift(
            node_last_updated, query_timestamp,
            drift_enabled=drift_enabled,
            drift_horizon_days=drift_horizon_days,
        )

        risk = error_term + beta * uncertainty_term + gamma * drift_term

    return CandidateNode(
        stats=stats,
        risk=risk,
        exact=exact,
        error_term=error_term,
        uncertainty_term=uncertainty_term,
        drift_term=drift_term,
    )


def _compute_drift(
    node_ts: str,
    query_ts: str,
    *,
    drift_enabled: bool = False,
    drift_horizon_days: float = 90.0,
) -> float:
    """Compute simple time-decay drift risk.

    D_C = min(1, age_in_days / drift_horizon_days)

    Returns 0.0 if drift is disabled or timestamps are missing.
    """
    if not drift_enabled:
        return 0.0
    if not node_ts or not query_ts:
        return 0.0

    try:
        # Parse ISO-format timestamps
        node_dt = _parse_iso(node_ts)
        query_dt = _parse_iso(query_ts)
        if node_dt is None or query_dt is None:
            return 0.0
        age_days = (query_dt - node_dt).total_seconds() / 86400.0
        if age_days < 0:
            return 0.0
        return min(1.0, age_days / drift_horizon_days)
    except (ValueError, TypeError):
        return 0.0


def _parse_iso(ts: str) -> datetime | None:
    """Parse an ISO-format timestamp string."""
    if not ts:
        return None
    try:
        # Handle 'Z' suffix
        s = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
