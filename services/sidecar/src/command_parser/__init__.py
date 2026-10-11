"""Declarative command grammar and deterministic KB feature projection."""

from .models import CommandFeatures, ParseContext
from .service import CommandFeatureService, default_service

__all__ = ["CommandFeatureService", "CommandFeatures", "ParseContext", "default_service"]
