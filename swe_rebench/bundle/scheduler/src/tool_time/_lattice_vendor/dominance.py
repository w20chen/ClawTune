"""Risk-aware dominance relations and frontier construction.

Defines:
- D ≻ C (D dominates C) when C ⊂ D and R_D ≤ R_C + δ
- The risk-aware frontier: all activated nodes not dominated by
  any other activated node.
"""

from __future__ import annotations

from typing import FrozenSet, List, Tuple

from tool_time._lattice_vendor.schemas import CandidateNode, DominanceEdge
from tool_time._lattice_vendor.normalize import FeatureSet


def compute_dominance(
    candidates: List[CandidateNode],
    delta: float = 0.05,
) -> Tuple[List[DominanceEdge], set[FeatureSet]]:
    """Compute dominance relations among activated candidates.

    For two nodes C and D:
        D dominates C iff C ⊂ D  AND  R_D ≤ R_C + δ

    Args:
        candidates: List of activated candidate nodes.
        delta: Allowable risk increase for more-specific nodes.

    Returns:
        Tuple of ``(edges, dominated_features)``.
    """
    edges: List[DominanceEdge] = []
    dominated: set[FeatureSet] = set()

    for general_cand in candidates:
        for specific_cand in candidates:
            if general_cand.stats.features == specific_cand.stats.features:
                continue
            # Strict subset: general ⊂ specific
            if general_cand.stats.features < specific_cand.stats.features:
                if specific_cand.risk <= general_cand.risk + delta:
                    dominated.add(general_cand.stats.features)
                    edges.append(DominanceEdge(
                        general_features=general_cand.stats.features,
                        specific_features=specific_cand.stats.features,
                    ))

    return edges, dominated


def construct_frontier(
    candidates: List[CandidateNode],
    edges: List[DominanceEdge],
    dominated: set[FeatureSet],
) -> List[CandidateNode]:
    """Construct the risk-aware frontier.

    The frontier contains all activated nodes that are NOT dominated
    by any other activated node.

    Args:
        candidates: All activated candidate nodes.
        edges: Dominance edges (for reference, not needed for construction).
        dominated: Set of feature sets that are dominated.

    Returns:
        Frontier candidates (non-dominated nodes).
    """
    frontier = [c for c in candidates if c.stats.features not in dominated]
    return frontier


def select_vertex(
    frontier: List[CandidateNode],
    *,
    specificity_risk_tolerance: float = 0.0,
) -> CandidateNode:
    """Select the best vertex from the frontier.

    When ``specificity_risk_tolerance > 0``, the most specific node
    (most features) is preferred as long as its risk is within
    ``specificity_risk_tolerance`` of the lowest-risk node.  This
    prevents overly general nodes from winning when a reasonably
    reliable specific node exists.

    Tie-breaking order:
    1. (With tolerance) Most specific node with acceptable risk, else lowest risk
    2. Highest sample count
    3. Most features
    4. Lexicographic order of sorted features (deterministic)

    Args:
        frontier: Non-dominated candidate nodes.
        specificity_risk_tolerance: Max extra risk allowed for the most
            specific node vs the lowest-risk node (default 0 = disabled).

    Returns:
        The selected candidate node.

    Raises:
        ValueError: If the frontier is empty.
    """
    if not frontier:
        raise ValueError("Frontier is empty; cannot select a vertex.")

    if specificity_risk_tolerance > 0 and len(frontier) > 1:
        lowest_risk = min(frontier, key=lambda c: c.risk)
        most_specific = max(frontier, key=lambda c: len(c.stats.features))

        if most_specific is not lowest_risk:
            if most_specific.risk <= lowest_risk.risk + specificity_risk_tolerance:
                # Prefer the more specific node since its risk is acceptable
                return min(
                    [c for c in frontier if c is most_specific or (
                        len(c.stats.features) == len(most_specific.stats.features)
                        and c.risk <= lowest_risk.risk + specificity_risk_tolerance
                    )],
                    key=lambda c: (c.risk, -c.stats.count, sorted(c.stats.features)),
                )

    return min(
        frontier,
        key=lambda c: (
            c.risk,
            -c.stats.count,
            -len(c.stats.features),
            sorted(c.stats.features),
        ),
    )
