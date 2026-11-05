"""lineage — provenance for everything a Hermes agent produces.

This package is the agent-side core: an observer that turns Hermes hook events
into a provenance graph (files, URLs, queries, tool calls, model calls, agents)
stored in SQLite, plus receipts that travel with the files the agent made.

Nothing in here changes what the agent does. Every hook is read-only, every
callback swallows its own errors, and the store never holds a secret on purpose.
"""

from __future__ import annotations

__version__ = "0.1.5"
RECEIPT_SCHEMA = "lineage.receipt.v1"
PLUGIN_ID = "lineage"

__all__ = ["__version__", "RECEIPT_SCHEMA", "PLUGIN_ID"]
