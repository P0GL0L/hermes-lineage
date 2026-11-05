"""Hashes, locators and the small text parsers lineage relies on."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Iterable, Iterator
from urllib.parse import urlsplit, urlunsplit

MAX_HASH_BYTES = 32 * 1024 * 1024  # refuse to hash files above this; record size only (keeps hooks fast)


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def sha256_file(path: str | os.PathLike[str]) -> tuple[str | None, int | None]:
    """Return ``(sha256, size)`` for *path*, or ``(None, size)`` when it cannot be hashed."""
    p = Path(path)
    try:
        size = p.stat().st_size
    except OSError:
        return None, None
    if size > MAX_HASH_BYTES:
        return None, size
    h = hashlib.sha256()
    try:
        with open(p, "rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                h.update(chunk)
    except OSError:
        return None, size
    return h.hexdigest(), size


from .redact import mask_query_secrets

_PATH_SECRET_RE = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")


def normalize_url(url: str) -> str:
    """Canonical form used for URL node ids: scheme/host lowercased, fragment and userinfo dropped,
    credential-shaped query values masked, trailing slash on a bare path removed. The query is
    otherwise kept because it identifies the page."""
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return redact_url_fallback(url.strip())
    scheme = (parts.scheme or "https").lower()
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    netloc = host
    try:
        if parts.port:
            netloc = f"{host}:{parts.port}"
    except ValueError:
        pass
    path = _PATH_SECRET_RE.sub("\u00abredacted\u00bb", parts.path or "/")
    if len(path) > 1 and path.endswith("/"):
        path = path[:-1]
    query = mask_query_secrets("?" + parts.query, "\u00abredacted\u00bb")[1:] if parts.query else ""
    return urlunsplit((scheme, netloc, path, query, ""))


def redact_url_fallback(url: str) -> str:
    return re.sub(r"(https?://)([^/\s@]+)@", r"\1", url)


def resolve_path(path: str, cwd: str | os.PathLike[str] | None) -> str:
    """Absolute, user-expanded, normalized path for *path* relative to *cwd*."""
    p = Path(os.path.expanduser(path))
    if not p.is_absolute() and cwd:
        p = Path(cwd) / p
    try:
        return str(p.resolve(strict=False))
    except (OSError, RuntimeError):
        return str(p.absolute())


# A backslash ends a URL: in shell commands and code, quotes around a URL are
# often escaped (\"http://…\"), and the escape is not part of the address.
_URL_RE = re.compile(r"https?://[^\s<>\"'`)\]}>,;\\]+")
_TRAILING_PUNCT = ".,;:!?)]}>'\""
# XML namespace names look like URLs but are identifiers nobody fetches
# (every SVG the agent writes names http://www.w3.org/2000/svg).
_XML_NAMESPACES = frozenset({
    "http://www.w3.org/2000/svg",
    "http://www.w3.org/1999/xhtml",
    "http://www.w3.org/1999/xlink",
    "http://www.w3.org/XML/1998/namespace",
    "http://www.w3.org/2000/xmlns",
    "http://www.w3.org/1998/Math/MathML",
})


def find_urls(text: str, limit: int = 200) -> list[str]:
    """URLs mentioned in *text*, de-duplicated, in order of first appearance."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _URL_RE.finditer(text or ""):
        u = m.group(0).rstrip(_TRAILING_PUNCT)
        if u.rstrip("/") in _XML_NAMESPACES:
            continue
        if u and u not in seen:
            seen.add(u)
            out.append(u)
            if len(out) >= limit:
                break
    return out


# Something that looks like a path: has a slash or a file extension, no spaces,
# not a URL. We later check it exists on disk, so false positives are cheap.
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w/.:-])((?:~|\.{1,2})?/?(?:[\w.\-@+]+/)*[\w.\-@+]+\.[A-Za-z0-9]{1,8}|(?:~|\.{1,2})?/(?:[\w.\-@+]+/)*[\w.\-@+]+)"
)


def find_path_candidates(text: str, limit: int = 400) -> list[str]:
    """Tokens in *text* that look like file paths. Callers verify them against disk."""
    seen: set[str] = set()
    out: list[str] = []
    for m in _PATH_TOKEN_RE.finditer(text or ""):
        tok = m.group(1).rstrip(_TRAILING_PUNCT)
        if "://" in tok or len(tok) < 3 or tok in seen:
            continue
        if tok.count(".") == len(tok):  # ".." or "."
            continue
        seen.add(tok)
        out.append(tok)
        if len(out) >= limit:
            break
    return out


def existing_paths(candidates: Iterable[str], cwd: str | None) -> Iterator[str]:
    """Yield absolute paths for the candidates that exist as regular files."""
    for cand in candidates:
        try:
            abs_path = resolve_path(cand, cwd)
            if os.path.isfile(abs_path):
                yield abs_path
        except (OSError, ValueError):
            continue


def is_probably_text(sample: bytes) -> bool:
    if not sample:
        return True
    if b"\x00" in sample:
        return False
    printable = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13) or b >= 128)
    return printable / len(sample) > 0.9


def short_id(value: str, length: int = 12) -> str:
    return sha256_text(value)[:length]


__all__ = [
    "sha256_bytes",
    "sha256_text",
    "sha256_file",
    "normalize_url",
    "resolve_path",
    "find_urls",
    "find_path_candidates",
    "existing_paths",
    "is_probably_text",
    "short_id",
    "MAX_HASH_BYTES",
]
