"""lineage — provenance receipts and a graph for everything Hermes produces.

Hooks only. No tools, no middleware, no network at runtime (``verify --refetch``
is the one explicit exception and is only ever run by you). See README.md.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from .lineage_core import PLUGIN_ID, __version__
from .lineage_core.store import Store
from .lineage_core.tracker import Tracker

log = logging.getLogger("hermes.plugins.lineage")

HOOKS = (
    "on_session_start",
    "on_session_end",
    "pre_llm_call",
    "post_llm_call",
    "post_api_request",
    "api_request_error",
    "post_auxiliary_call",
    "pre_tool_call",
    "post_tool_call",
    "subagent_start",
    "subagent_stop",
)

CONFIG_KEYS = {
    "sidecar": bool,
    "store_previews": bool,
    "track_aux_calls": bool,
    "snapshot_max_files": int,
    "session_window_turns": int,
}

_tracker: Optional[Tracker] = None


def _read_config(ctx) -> dict[str, Any]:
    cfg: dict[str, Any] = {}
    for key, typ in CONFIG_KEYS.items():
        try:
            value = ctx.get_config(key, None)
        except Exception:
            value = None
        if value is None:
            continue
        try:
            cfg[key] = typ(value) if typ is not bool else (str(value).lower() in ("1", "true", "yes", "on") if isinstance(value, str) else bool(value))
        except (TypeError, ValueError):
            continue
    return cfg


def _emit(event: str, payload: dict) -> None:
    """Push to the Desktop half through Hermes's public event bridge, when it exists."""
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event  # documented public bridge
    except Exception:
        return
    try:
        broadcast_plugin_event(PLUGIN_ID, event, payload)
    except Exception:
        pass


def _cwd() -> Optional[str]:
    try:
        from agent.runtime_cwd import resolve_agent_cwd  # documented helper

        return str(resolve_agent_cwd())
    except Exception:
        return None


def get_tracker(config: Optional[dict] = None) -> Tracker:
    """The process-wide tracker, created on first use so register() stays instant."""
    global _tracker
    if _tracker is None:
        _tracker = Tracker(Store.shared(), config or {}, emit=_emit, cwd_resolver=_cwd)
    return _tracker


def register(ctx) -> None:
    cfg = _read_config(ctx)

    def hook(name: str):
        def _cb(**kwargs):
            try:
                getattr(get_tracker(cfg), name)(**kwargs)
            except Exception as exc:  # the tracker already guards; this guards the tracker
                log.debug("lineage hook %s: %s", name, exc)

        _cb.__name__ = f"lineage_{name}"
        return _cb

    for name in HOOKS:
        ctx.register_hook(name, hook(name))

    def slash(raw_args: str = "", session_id: Optional[str] = None, **_kw) -> str:
        from .lineage_core.commands import slash_handler

        try:
            return slash_handler(get_tracker(cfg).store, raw_args, session_id=session_id, cwd=_cwd())
        except Exception as exc:
            return f"lineage: {exc.__class__.__name__}: {exc}"

    ctx.register_command(
        "lineage",
        slash,
        description="Where did this file come from? Provenance for what the agent produced.",
        args_hint="[file | receipt <file> | verify <file> | mermaid <file> | stats]",
    )

    def cli_setup(parser):
        from .lineage_core.commands import cli_setup as _setup

        _setup(parser)

    def cli_handler(args):
        from .lineage_core.commands import cli_handler as _handler

        return _handler(args, store_factory=lambda: get_tracker(cfg).store)

    ctx.register_cli_command(
        "lineage",
        "Provenance for files the agent produced: list, show, receipt, verify, graph",
        cli_setup,
        cli_handler,
        description=f"lineage {__version__}: provenance receipts and graphs for agent-made files.",
    )


__all__ = ["register", "get_tracker", "HOOKS", "__version__"]
