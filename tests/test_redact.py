from __future__ import annotations

from importlib import import_module

import pytest


@pytest.fixture
def redact(plugin):
    return import_module(f"{plugin.__name__}.lineage_core.redact")


def test_prefix_tokens_are_masked(redact):
    body = "use key " + "sk" + "-" + "A" * 40 + " and " + "gh" + "p_" + "b" * 36 + " please"
    out = redact.redact_text(body)
    assert "A" * 40 not in out and "b" * 36 not in out
    assert out.count(redact.MASK) == 2


def test_key_value_and_bearer(redact):
    out = redact.redact_text('{"api_key": "abcdefghijkl12345", "token":"zzzzzzzzzz"} Authorization: Bearer abcdefghijklmnop')
    assert "abcdefghijkl12345" not in out and "zzzzzzzzzz" not in out and "abcdefghijklmnop" not in out


def test_env_assignment_and_url_credentials(redact):
    out = redact.redact_text('export OPENAI_API_KEY="hunter2hunter2" && curl https://user:p4ssw0rd@host/x?token=abcdefgh1234')
    assert "hunter2hunter2" not in out and "p4ssw0rd" not in out and "abcdefgh1234" not in out
    assert "https://user:" in out


def test_pem_block(redact):
    out = redact.redact_text("-----BEGIN RSA PRIVATE KEY-----\nMIIE...\n-----END RSA PRIVATE KEY-----")
    assert "MIIE" not in out and out == redact.MASK


def test_plain_text_untouched(redact):
    text = "Revenue grew 18% in Q3; see revenue.csv and https://example.com/report"
    assert redact.redact_text(text) == text


def test_bound_args_shapes(redact):
    args = {"path": "a.md", "content": "x" * 5000, "password": "secret-value-1", "nested": {"api_key": "k" * 30, "n": 3},
            "list": list(range(100))}
    out = redact.bound_args(args)
    assert out["path"] == "a.md"
    assert out["content"]["$chars"] == 5000 and len(out["content"]["$preview"]) <= 400
    assert out["password"] == redact.MASK and out["nested"]["api_key"] == redact.MASK and out["nested"]["n"] == 3
    assert out["list"][-1] == {"$more": 68}


def test_preview_is_single_line_and_bounded(redact):
    assert redact.preview("a\n\n b   c", 100) == "a b c"
    assert len(redact.preview("x" * 1000, 50)) == 50
