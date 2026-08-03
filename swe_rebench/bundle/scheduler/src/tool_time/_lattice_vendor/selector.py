"""Vertex selector: activate, score, dominate, and select.

This is the core prediction pipeline. Two strategies are available:

1. **risk-based** (legacy, context_variance, shrinkage):
   Activate → compute risk → dominance → frontier → select vertex.

2. **reliable-specificity** (loso):
   Activate all matching nodes → sort by reliability then specificity → pick first.
   Clean, single-pass selection with no dominance/frontier/tolerance.
"""

from __future__ import annotations

from typing import Dict, List

from tool_time._lattice_vendor.dominance import compute_dominance, construct_frontier, select_vertex
from tool_time._lattice_vendor.nodes import get_estimate
from tool_time._lattice_vendor.normalize import FeatureSet, normalize_command
from tool_time._lattice_vendor.risk import compute_candidate_risk
from tool_time._lattice_vendor.schemas import CandidateNode, DominanceEdge, NodeStats, PredictionResult


def _predict_reliable_specificity(
    cmd: str,
    query_features: FeatureSet,
    nodes: Dict[FeatureSet, NodeStats],
    *,
    global_log_std: float = 0.5,
    loso_min_signatures: int = 2,
    risk_weight: float = 1.0,
    estimator: str = "median",
) -> PredictionResult:
    """Predict using the Reliable Specificity strategy.

    Score each candidate node as::

        score = |C| − risk_weight × risk

    where risk = LOSO risk (or 999 for sig_count=1 nodes).
    Higher score = better balance of relevance and reliability.

    - ``risk_weight = 0`` → pure max_cardinality (ignore risk)
    - ``risk_weight = 1`` → equal weight (default)
    - ``risk_weight ≫ 1`` → pure risk minimization

    This single linear score replaces the entire
    risk → dominance → frontier → tolerance pipeline.
    """
    # ── Activate: collect all matching nodes ────────────────────────
    candidates: List[NodeStats] = []
    exact_stats: NodeStats | None = None
    for fs, stats in nodes.items():
        if fs.issubset(query_features):
            candidates.append(stats)
            if fs == query_features:
                exact_stats = stats

    if not candidates:
        return _fallback_result(cmd, query_features, nodes, global_log_std, estimator)

    # ── Score: specificity minus weighted risk ───────────────────────
    # For exact-match nodes, risk is based on sample count (not sig_count,
    # which is always 1 for exact nodes).  Multi-sample exact nodes get
    # per-sample LOO risk; single-sample exact nodes get a mild penalty
    # so they don't blindly win but can still compete.
    def _risk(stats: NodeStats) -> float:
        if stats.signature_count >= 2:
            return stats.loso_risk if stats.loso_risk > 0 else 0.0
        # sig_count = 1: LOSO not computable
        if stats.features == query_features and stats.count >= 2:
            return stats.loo_mse_log  # per-sample LOO for exact multi-sample
        return 999.0  # heavy penalty for single-sig partial nodes

    def _score(stats: NodeStats) -> float:
        return len(stats.features) - risk_weight * _risk(stats)

    candidates.sort(key=lambda s: (-_score(s), -s.count))
    best = candidates[0]

    # ── Build result ────────────────────────────────────────────────
    prediction_s = get_estimate(best, estimator=estimator)
    is_exact = (best.features == query_features)

    return PredictionResult(
        cmd=cmd,
        prediction_s=prediction_s,
        exact_match=is_exact,
        query_features=sorted(query_features),
        selected_features=sorted(best.features),
        selected_sample_count=best.count,
        selected_risk=_risk(best),
        activated_count=len(candidates),
        frontier_count=1,  # no frontier in this strategy
        activated_nodes=[],
        frontier_nodes=[],
        dominance_edges=[],
        fallback="",
        estimator=estimator,
    )


def predict(
    cmd: str,
    nodes: Dict[FeatureSet, NodeStats],
    *,
    repo: str | None = None,
    cwd: str | None = None,
    env_id: str | None = None,
    global_log_var: float = 0.0,
    global_log_std: float = 0.5,
    beta: float = 0.5,
    gamma: float = 0.3,
    delta: float = 0.05,
    exact_singleton_risk: float = 0.03,
    risk_method: str = "context_variance",
    context_shrinkage_m: float = 3.0,
    context_sample_alpha: float = 0.03,
    drift_enabled: bool = False,
    drift_horizon_days: float = 90.0,
    estimator: str = "median",
    timestamp: str = "",
    shrinkage_kappa: float = 5.0,
    exact_match_shortcut: bool = False,
    loso_min_signatures: int = 3,
    specificity_risk_tolerance: float = 0.0,
    risk_weight: float = 1.0,
) -> PredictionResult:
    """Predict execution time for a command.

    Args:
        cmd: The shell command to predict.
        nodes: All historical context nodes with statistics.
        repo: Repository identifier for this query.
        cwd: Working directory.
        env_id: Environment identifier.
        global_log_var: Global log-duration variance.
        global_log_std: Global log-duration standard deviation.
        beta: Uncertainty weight.
        gamma: Drift weight.
        delta: Dominance tolerance.
        exact_singleton_risk: Risk for exact single-sample nodes.
        drift_enabled: Enable time-based drift.
        drift_horizon_days: Drift horizon in days.
        estimator: Point estimator (``"median"``, ``"mean"``, ``"geometric_mean"``).
        timestamp: ISO timestamp of the query.
        shrinkage_kappa: Shrinkage strength for ``"shrinkage"`` risk method.
        exact_match_shortcut: If True, when the query's exact feature set
            exists in history, use its median directly, bypassing the
            risk/dominance/frontier pipeline.
        loso_min_signatures: Minimum distinct signatures for ``"loso"``
            risk method (default 2).  Nodes with fewer signatures are
            penalized with high risk.
        risk_weight: Weight of LOSO risk in the combined score for
            ``"loso"`` method (default 1.0).
            score = |C| − risk_weight × risk.
            0 = pure specificity; larger = more risk-averse.

    Returns:
        ``PredictionResult`` with prediction, frontier, and explanation data.
    """
    query_features, _ = normalize_command(cmd, repo=repo, cwd=cwd, env_id=env_id)

    # ── LOSO uses the clean Reliable Specificity strategy ─────────────
    if risk_method == "loso":
        return _predict_reliable_specificity(
            cmd, query_features, nodes,
            global_log_std=global_log_std,
            loso_min_signatures=loso_min_signatures,
            risk_weight=risk_weight,
            estimator=estimator,
        )

    # ── Exact match shortcut ───────────────────────────────────────────
    # Shrinkage and LOSO methods auto-enable this shortcut because their
    # risk formulas don't treat exact matches specially.  For legacy and
    # context_variance, it must be explicitly enabled via the flag.
    _use_shortcut = (
        exact_match_shortcut or risk_method in ("shrinkage", "loso")
    )
    if _use_shortcut and query_features in nodes:
        exact_stats = nodes[query_features]
        prediction_s = get_estimate(exact_stats, estimator=estimator)
        return PredictionResult(
            cmd=cmd,
            prediction_s=prediction_s,
            exact_match=True,
            query_features=sorted(query_features),
            selected_features=sorted(query_features),
            selected_sample_count=exact_stats.count,
            selected_risk=0.0,
            activated_count=1,
            frontier_count=1,
            activated_nodes=[],
            frontier_nodes=[],
            dominance_edges=[],
            fallback="",
            estimator=estimator,
        )

    # ── Step 1: Activate nodes ────────────────────────────────────────
    activated: List[CandidateNode] = []
    activated_features: set[FeatureSet] = set()
    for fs, stats in nodes.items():
        if fs.issubset(query_features):
            # LOSO: skip nodes with too few distinct signatures
            if risk_method == "loso" and stats.signature_count < loso_min_signatures:
                continue
            candidate = compute_candidate_risk(
                stats,
                query_features,
                global_log_var=global_log_var,
                global_log_std=global_log_std,
                beta=beta,
                gamma=gamma,
                exact_singleton_risk=exact_singleton_risk,
                risk_method=risk_method,
                context_shrinkage_m=context_shrinkage_m,
                context_sample_alpha=context_sample_alpha,
                drift_enabled=drift_enabled,
                drift_horizon_days=drift_horizon_days,
                node_last_updated=stats.last_updated,
                query_timestamp=timestamp,
                shrinkage_kappa=shrinkage_kappa,
                loso_min_signatures=loso_min_signatures,
            )
            activated.append(candidate)
            activated_features.add(fs)

    # ── LOSO max-cardinality rescue ───────────────────────────────────
    # Nodes with sig_count < m_min are normally excluded.  But the
    # *most specific* matching node (max cardinality) is often the
    # most relevant — excluding it forces selection of overly general
    # nodes.  We rescue it with a fair fallback risk.
    _mc_rescued = False
    if risk_method == "loso":
        # Find max-cardinality node (most features matching query)
        mc_fs: FeatureSet | None = None
        mc_stats: NodeStats | None = None
        mc_best = -1
        for fs, stats in nodes.items():
            if fs.issubset(query_features) and len(fs) > mc_best:
                mc_best = len(fs)
                mc_fs = fs
                mc_stats = stats

        # If the max-cardinality node was filtered out, rescue it
        if mc_fs is not None and mc_fs not in activated_features and mc_stats is not None:
            # Compute a fair fallback risk for this sig_count=1 node
            if mc_stats.count >= 2:
                _rescue_risk = mc_stats.loo_mse_log  # per-sample LOO is valid
            else:
                _rescue_risk = global_log_var  # single sample, use global prior

            rescued = CandidateNode(
                stats=mc_stats,
                risk=_rescue_risk,
                exact=(mc_stats.features == query_features),
                error_term=_rescue_risk,
                uncertainty_term=0.0,
                drift_term=0.0,
            )
            activated.append(rescued)
            _mc_rescued = True

    # ── Fallback: no activated nodes ───────────────────────────────────
    if not activated:
        return _fallback_result(cmd, query_features, nodes, global_log_std, estimator)

    # ── Step 2: Dominance ──────────────────────────────────────────────
    edges, dominated = compute_dominance(activated, delta=delta)

    # ── Step 3: Frontier ───────────────────────────────────────────────
    frontier = construct_frontier(activated, edges, dominated)

    # ── Step 4: Select ─────────────────────────────────────────────────
    selected = select_vertex(frontier, specificity_risk_tolerance=specificity_risk_tolerance)

    # ── Build result ───────────────────────────────────────────────────
    prediction_s = get_estimate(selected.stats, estimator=estimator)

    return PredictionResult(
        cmd=cmd,
        prediction_s=prediction_s,
        exact_match=selected.exact,
        query_features=sorted(query_features),
        selected_features=sorted(selected.stats.features),
        selected_sample_count=selected.stats.count,
        selected_risk=selected.risk,
        activated_count=len(activated),
        frontier_count=len(frontier),
        activated_nodes=activated,
        frontier_nodes=frontier,
        dominance_edges=edges,
        fallback="",
        estimator=estimator,
    )


def _fallback_result(
    cmd: str,
    query_features: FeatureSet,
    nodes: Dict[FeatureSet, NodeStats],
    global_log_std: float,
    estimator: str,
) -> PredictionResult:
    """Generate a fallback result when no nodes match.

    Attempts tool-level fallback first, then global median.
    """
    # Try tool-only matching
    tool_features = {f for f in query_features if f.startswith("tool=")}
    tool_nodes = [
        (fs, s) for fs, s in nodes.items()
        if tool_features and tool_features.issubset(fs)
    ]
    if tool_nodes:
        # Use the best tool-level node
        best_fs, best_stats = max(tool_nodes, key=lambda x: x[1].count)
        pred = get_estimate(best_stats, estimator=estimator)
        return PredictionResult(
            cmd=cmd,
            prediction_s=pred,
            exact_match=False,
            query_features=sorted(query_features),
            selected_features=sorted(best_fs),
            selected_sample_count=best_stats.count,
            selected_risk=0.0,
            activated_count=0,
            frontier_count=0,
            activated_nodes=[],
            frontier_nodes=[],
            dominance_edges=[],
            fallback="tool",
            estimator=estimator,
        )

    # Global fallback: use overall median from nodes
    all_durations = []
    for s in nodes.values():
        all_durations.extend(s.durations)
    if all_durations:
        import statistics
        pred = statistics.median(all_durations)
    else:
        pred = 1.0

    return PredictionResult(
        cmd=cmd,
        prediction_s=pred,
        exact_match=False,
        query_features=sorted(query_features),
        selected_features=[],
        selected_sample_count=len(all_durations),
        selected_risk=0.0,
        activated_count=0,
        frontier_count=0,
        activated_nodes=[],
        frontier_nodes=[],
        dominance_edges=[],
        fallback="global",
        estimator=estimator,
    )
