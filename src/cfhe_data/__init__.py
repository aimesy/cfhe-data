"""CFHE data pipeline."""

from .dedupe import deduplicate
from .models import DedupResult, PermitRecord
from .pipeline import build_artifacts

__version__ = "0.1.0"

__all__ = [
    "DedupResult",
    "PermitRecord",
    "build_artifacts",
    "deduplicate",
]
