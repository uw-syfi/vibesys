"""Pure immutable parent catalog. Fitness never conveys adoption authority.

Ingest accepts independently retained, exact trusted accuracy receipts.
Options derive latest chronology and best comparable observed partials;
resolve selects exact offered revisions without substituting another parent.
"""

from vibesys.orchestration.dynamic.parents._catalog import (
    ParentCatalog,
    ParentComparisonKey,
    ParentOption,
    ParentSnapshot,
    ingest,
    options,
    resolve,
)

__all__ = [
    "ParentCatalog",
    "ParentComparisonKey",
    "ParentOption",
    "ParentSnapshot",
    "ingest",
    "options",
    "resolve",
]
