"""Test fixtures: load the plugin the way Hermes does (a package built from __init__.py),
stand up an isolated HERMES_HOME, and drive hooks with the documented payload shapes."""

from __future__ import annotations

import importlib.util
import json
import sys
import time
import uuid
from pathlib import Path

import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]


def load_plugin_package(name: str = "hermes_plugin_lineage_under_test"):
    """Mirror hermes_cli/plugins_loader: spec from __init__.py with the plugin dir as package path."""
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(
        name, PLUGIN_DIR / "__init__.py", submodule_search_locations=[str(PLUGIN_DIR)]
    )
    module = importlib.util.module_from_spec(spec)
    module.__path__ = [str(PLUGIN_DIR)]
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def plugin():
    return load_plugin_package()


@pytest.fixture
def core(plugin):
    import importlib

    return importlib.import_module(plugin.__name__ + ".lineage_core")


@pytest.fixture
def hermes_home(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


@pytest.fixture
def store(core, hermes_home):
    from importlib import import_module

    Store = import_module(core.__name__ + ".store").Store
    db = hermes_home / "plugin-data" / "lineage" / "lineage.db"
    s = Store(db)
    yield s
    s.close()


@pytest.fixture
def workspace(tmp_path):
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


class RecordingContext:
    """The validator's probe context, reproduced: register() must work against exactly this."""

    plugin_config: dict = {}
    profile_name = "default"
    plugin_id = "hermes_validate_probe_plugin"

    def __init__(self, context_methods=()):
        self.recorded = {"tools": [], "hooks": [], "middleware": [], "commands": [], "cli_commands": []}
        self.callbacks: dict[str, list] = {}
        self.slash = None
        self.cli = None
        self._context_methods = set(context_methods)

    def register_tool(self, name, *args, **kwargs):
        self.recorded["tools"].append(str(name))

    def register_hook(self, hook_name, callback, *, hold_stream=False):
        self.recorded["hooks"].append(str(hook_name))
        self.callbacks.setdefault(hook_name, []).append(callback)

    def register_middleware(self, kind, callback):
        self.recorded["middleware"].append(str(kind))

    def register_command(self, name, handler, *args, **kwargs):
        self.recorded["commands"].append(str(name))
        self.slash = handler

    def register_cli_command(self, name, help, setup_fn, handler_fn=None, **kwargs):
        self.recorded["cli_commands"].append(str(name))
        self.cli = (setup_fn, handler_fn)

    def get_config(self, key, default=None):
        return default

    def __getattr__(self, name):
        if name in self._context_methods:
            return lambda *a, **k: None
        raise AttributeError(name)

    # --- test helper: fire a hook the way PluginManager does (kwargs only) ---
    def fire(self, hook_name, **payload):
        payload.setdefault("telemetry_schema_version", "hermes.observer.v1")
        for cb in self.callbacks.get(hook_name, []):
            cb(**payload)


@pytest.fixture
def ctx(plugin, hermes_home, monkeypatch):
    # fresh tracker per test
    monkeypatch.setattr(plugin, "_tracker", None)
    c = RecordingContext()
    plugin.register(c)
    return c


class Session:
    """A scripted Hermes session that emits realistic hook payloads."""

    def __init__(self, ctx, workspace: Path, session_id: str | None = None, model: str = "hermes-4-405b",
                 platform: str = "cli", parent_session_id: str | None = None):
        self.ctx = ctx
        self.ws = workspace
        self.sid = session_id or f"sess-{uuid.uuid4().hex[:8]}"
        self.model = model
        self.platform = platform
        self.parent = parent_session_id
        self.turn_id = None
        self.api_n = 0
        self.ctx.fire("on_session_start", session_id=self.sid, model=model, platform=platform, cwd=str(workspace),
                      parent_session_id=parent_session_id, profile_name="default", provider="nous")

    def start_turn(self, user_message: str):
        self.turn_id = f"turn-{uuid.uuid4().hex[:8]}"
        self.ctx.fire("pre_llm_call", session_id=self.sid, task_id=None, turn_id=self.turn_id, user_message=user_message,
                      conversation_history=[], is_first_turn=True, model=self.model, platform=self.platform,
                      parent_session_id=self.parent, sender_id="u1")
        return self.turn_id

    def model_call(self, tool_calls: int = 1):
        self.api_n += 1
        rid = f"req-{self.sid}-{self.api_n}"
        now = time.time()
        self.ctx.fire("post_api_request", session_id=self.sid, task_id=None, turn_id=self.turn_id, api_request_id=rid,
                      platform=self.platform, model=self.model, provider="nous", base_url="https://inference-api.nousresearch.com/v1",
                      api_mode="chat_completions", api_call_count=self.api_n, api_duration=1.2, started_at=now - 1.2, ended_at=now,
                      finish_reason="tool_calls", message_count=4, response_model=self.model,
                      usage={"prompt_tokens": 1200, "completion_tokens": 80}, assistant_tool_call_count=tool_calls,
                      response={"choices": []}, assistant_message={})
        return rid

    def tool(self, name: str, args: dict, result, *, during=None, status: str = "ok", api_request_id: str | None = None):
        """pre_tool_call -> (during: side effects) -> post_tool_call."""
        tcid = f"call_{uuid.uuid4().hex[:10]}"
        rid = api_request_id or f"req-{self.sid}-{self.api_n}"
        self.ctx.fire("pre_tool_call", tool_name=name, args=args, task_id=None, session_id=self.sid, tool_call_id=tcid,
                      turn_id=self.turn_id, api_request_id=rid, middleware_trace=[])
        t0 = time.time()
        if during:
            during()
        if not isinstance(result, str):
            result = json.dumps(result)
        self.ctx.fire("post_tool_call", tool_name=name, args=args, result=result, task_id=None, session_id=self.sid,
                      tool_call_id=tcid, turn_id=self.turn_id, api_request_id=rid, duration_ms=int((time.time() - t0) * 1000) + 5,
                      status=status, error_type=None, error_message=None, middleware_trace=[])
        return tcid

    def end_turn(self, assistant_response: str):
        self.ctx.fire("post_llm_call", session_id=self.sid, task_id=None, turn_id=self.turn_id, user_message="",
                      assistant_response=assistant_response, conversation_history=[], model=self.model, platform=self.platform,
                      side_question_token=None)

    def end(self):
        self.ctx.fire("on_session_end", session_id=self.sid, completed=True, interrupted=False)


@pytest.fixture
def session_factory(ctx, workspace):
    def make(**kw):
        return Session(ctx, workspace, **kw)

    return make
