"""TDD (R4a): redaction of anything that reaches disk.

``config.local.yaml`` holds a real API key, and a trace records tool parameters
verbatim.  So the redactor runs on the write path, not as an afterthought
(docs/r4-analysis.md 4.5).
"""

import pytest

from ai_coding.core.trace import Redactor


def test_openai_style_key_is_redacted():
    out = Redactor().redact('key = "sk-abcdefghijklmnopqrstuvwxyz012345"')
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in out
    assert "redacted" in out


def test_github_and_aws_tokens_are_redacted():
    redactor = Redactor()
    gh = "ghp_abcdefghijklmnopqrstuvwxyz0123456789"
    aws = "AKIAIOSFODNN7EXAMPLE"
    assert gh not in redactor.redact(f"token {gh} end")
    assert aws not in redactor.redact(f"id {aws} end")


def test_key_value_shapes_are_redacted():
    out = Redactor().redact("api_key: super-secret-value\npassword=hunter2hunter2")
    assert "super-secret-value" not in out
    assert "hunter2hunter2" not in out


def test_private_key_block_is_redacted_whole():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEA1234567890abcdefghij\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = Redactor().redact(pem)
    assert "MIIEowIBAAKCAQEA" not in out
    assert out.count("redacted") == 1


def test_benign_text_is_untouched():
    text = "read README.md and run pytest -q in the workspace"
    assert Redactor().redact(text) == text


def test_redaction_is_idempotent():
    redactor = Redactor()
    once = redactor.redact("key sk-abcdefghijklmnopqrstuvwxyz012345 end")
    assert redactor.redact(once) == once


def test_redact_obj_walks_nested_structures():
    payload = {
        "command": "curl -H 'sk-abcdefghijklmnopqrstuvwxyz012345'",
        "nested": [{"token": "ghp_abcdefghijklmnopqrstuvwxyz0123456789"}],
        "count": 3,
        "none": None,
    }
    out = Redactor().redact_obj(payload)
    assert "sk-abcdefghijklmnopqrstuvwxyz012345" not in str(out)
    assert "ghp_abcdefghijklmnopqrstuvwxyz0123456789" not in str(out)
    # non-strings survive unchanged
    assert out["count"] == 3
    assert out["none"] is None


def test_disabled_redactor_is_a_passthrough():
    secret = "sk-abcdefghijklmnopqrstuvwxyz012345"
    assert Redactor(enabled=False).redact(secret) == secret


@pytest.mark.parametrize("blank", ["", "   ", "plain text with no secrets"])
def test_blank_and_plain_inputs_are_stable(blank):
    assert Redactor().redact(blank) == blank
