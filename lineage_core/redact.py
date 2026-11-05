"""Redaction and bounding for anything lineage writes to disk.

lineage records *that* a tool ran and *what it touched*, not what it said. Tool
arguments and results are reduced to bounded previews, and anything that looks
like a credential is replaced before the bytes reach SQLite. The patterns here
are deliberately broad: lineage prefers to redact a harmless string over storing
a secret, and nothing downstream (graph, receipts, Desktop page) needs the
original text.
"""

from __future__ import annotations

import re
from typing import Any

MASK = "«redacted»"

# Vendor prefixes are assembled from parts so a scanner that greps this file for
# credential shapes does not mistake the detector for a leaked credential.
_PREFIXES = [
    "sk" + "-ant-",
    "sk" + "-",
    "gh" + "p_",
    "gh" + "o_",
    "gh" + "u_",
    "gh" + "s_",
    "gh" + "r_",
    "github" + "_pat_",
    "gl" + "pat-",
    "xox" + "b-",
    "xox" + "p-",
    "xox" + "a-",
    "AK" + "IA",
    "AI" + "za",
    "np" + "m_",
    "py" + "pi-",
    "hf" + "_",
    "r8" + "_",
    "fal" + "_",
    "sb" + "p_",
    "shp" + "at_",
    "sq0" + "atp-",
    "ey" + "J",  # JWT header start (base64 of '{"')
]

_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_/+-])(?:" + "|".join(re.escape(p) for p in _PREFIXES) + r")[A-Za-z0-9_\-./+=]{16,}"
)

# key = value / key: value / "key": "value" with a credential-looking key name.
_KV_RE = re.compile(
    r"(?i)(\b(?:api[_-]?key|access[_-]?key|secret[_-]?key|client[_-]?secret|private[_-]?key|"
    r"auth[_-]?token|access[_-]?token|refresh[_-]?token|id[_-]?token|session[_-]?token|"
    r"token|secret|password|passwd|pwd|credential|authorization|cookie|set-cookie)"
    r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&]{6,})"
)

_BEARER_RE = re.compile(r"(?i)\b(bearer|basic|token)\s+([A-Za-z0-9_\-./+=]{12,})")
_PEM_RE = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)
_URL_CRED_RE = re.compile(r"(https?://)([^/\s:@]+):([^/\s@]+)@")
_URL_QUERY_SECRET_RE = re.compile(
    r"(?i)([?&](?:api[_-]?key|key|token|secret|access[_-]?token|sig|signature|password)=)([^&\s#]+)"
)

# Env-var style assignments inside shell commands: FOO_TOKEN=abc, export X_KEY="..."
_ENV_ASSIGN_RE = re.compile(
    r"(?i)\b([A-Z][A-Z0-9_]*(?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIALS?)[A-Z0-9_]*\s*=\s*)([\"']?)([^\s\"']{4,})(\2)"
)

_SECRET_ARG_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "token",
        "secret",
        "password",
        "passwd",
        "authorization",
        "auth",
        "cookie",
        "cookies",
        "headers",
        "credentials",
        "private_key",
    }
)


def redact_text(text: str) -> str:
    """Return *text* with credential-shaped substrings masked."""
    if not text:
        return text
    out = _PEM_RE.sub(MASK, text)
    out = _URL_CRED_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}:{MASK}@", out)
    out = _URL_QUERY_SECRET_RE.sub(lambda m: f"{m.group(1)}{MASK}", out)
    out = _ENV_ASSIGN_RE.sub(lambda m: f"{m.group(1)}{m.group(2)}{MASK}{m.group(4)}", out)
    out = _BEARER_RE.sub(lambda m: f"{m.group(1)} {MASK}", out)
    out = _KV_RE.sub(lambda m: f"{m.group(1)}{MASK}", out)
    out = _PREFIX_RE.sub(MASK, out)
    return out


def preview(text: Any, limit: int = 240) -> str:
    """A short, redacted, single-line preview of *text*."""
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            return ""
    text = redact_text(text)
    text = " ".join(text.split())
    if len(text) > limit:
        return text[: limit - 1] + "…"
    return text


def bound_args(args: Any, *, per_value: int = 400, total: int = 4000, depth: int = 0) -> Any:
    """Return a JSON-safe, redacted, size-bounded copy of tool arguments.

    Long string values become ``{"$preview": ..., "$chars": n}`` so the shape of
    the call survives without its payload (a 40 KB ``write_file.content`` is
    recorded as its length and a redacted first line, never verbatim).
    """
    if depth > 6:
        return "…"
    if isinstance(args, dict):
        out: dict[str, Any] = {}
        budget = total
        for key, value in list(args.items())[:64]:
            k = str(key)
            if k.lower() in _SECRET_ARG_NAMES:
                out[k] = MASK
                continue
            bounded = bound_args(value, per_value=per_value, total=budget, depth=depth + 1)
            out[k] = bounded
            budget -= len(str(bounded))
            if budget <= 0:
                out["$truncated"] = True
                break
        return out
    if isinstance(args, (list, tuple)):
        items = [bound_args(v, per_value=per_value, total=total, depth=depth + 1) for v in list(args)[:32]]
        if len(args) > 32:
            items.append({"$more": len(args) - 32})
        return items
    if isinstance(args, str):
        if len(args) > per_value:
            return {"$preview": preview(args, per_value), "$chars": len(args)}
        return redact_text(args)
    if isinstance(args, (int, float, bool)) or args is None:
        return args
    return preview(args, per_value)


__all__ = ["MASK", "redact_text", "preview", "bound_args"]
