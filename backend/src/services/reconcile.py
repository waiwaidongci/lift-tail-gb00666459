"""Compatibility alias for the reconciliation domain service."""

from src.services.reconciliation import (  # noqa: F401
    build_reconciliation,
    lookup_record,
    match_records,
    reconcile,
    reconcile_records,
    run_reconciliation,
)
