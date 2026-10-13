"""Configuration shared by feature-combination knowledge bases."""

DEFAULT_MAX_OPTIONAL_FEATURES = 6


def validate_max_optional_features(value: int) -> int:
    """Bound partial combinations, while always retaining full signatures."""
    if type(value) is not int or value < 0:
        raise ValueError("max_optional_features must be a nonnegative integer")
    return value


def require_same_feature_limit(actual: int, expected: int, *, source: str) -> None:
    validate_max_optional_features(actual)
    validate_max_optional_features(expected)
    if actual != expected:
        raise ValueError(
            f"{source} max_optional_features={actual}, configured={expected}; "
            "Lattice and EdgeKappa must use the same limit. Use matching snapshots "
            "or explicitly rebuild a new seed from raw observations; "
            "existing EdgeKappa weights cannot be relabeled."
        )
