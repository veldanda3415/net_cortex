"""Catalog-driven detection: rule evaluation, domain analysis, correlation.

All functions here are pure (inputs in, finding out); the catalog
(contract/catalog/) supplies every threshold, template and mapping.
"""

from detection.analyze import analyze_domain
from detection.correlation import reconsider

__all__ = ["analyze_domain", "reconsider"]
