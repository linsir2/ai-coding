"""M-H2: where the key comes from.

The order is `${VAR}` → literal → the provider's conventional variable.  The
environment fills an *absence*; it never overrules a key the operator wrote out.
That distinction is the whole point: a CI machine with `DEEPSEEK_API_KEY` exported
must not silently send someone else's account's key because a config file
happened to contain a literal.
"""

from __future__ import annotations

import pytest

from ai_coding.config.secrets import (
    MissingSecret,
    resolve_api_key,
    secret_env_names,
)


def test_an_env_var_reference_resolves():
    assert resolve_api_key("${FOO}", "generic", env={"FOO": "k"}) == "k"


def test_a_missing_env_reference_is_a_hard_error():
    """Today this silently became "", and the failure surfaced at the SDK."""
    with pytest.raises(MissingSecret) as caught:
        resolve_api_key("${FOO}", "generic", env={})

    assert "FOO" in str(caught.value)


def test_an_empty_env_reference_is_a_hard_error():
    """An exported-but-empty variable is the classic CI accident."""
    with pytest.raises(MissingSecret):
        resolve_api_key("${FOO}", "generic", env={"FOO": ""})


def test_a_literal_key_is_never_overridden_by_the_environment():
    """Regression pin: env fills an absence, it does not overrule a written key."""
    assert (
        resolve_api_key("sk-test", "deepseek", env={"DEEPSEEK_API_KEY": "other"})
        == "sk-test"
    )


def test_the_provider_env_var_fills_an_absent_key():
    assert resolve_api_key("", "deepseek", env={"DEEPSEEK_API_KEY": "k"}) == "k"


def test_no_key_anywhere_is_the_empty_string():
    """Not an error here: the caller decides, and today it is the SDK's job."""
    assert resolve_api_key("", "deepseek", env={}) == ""


def test_a_padded_reference_is_still_a_reference():
    assert resolve_api_key("  ${FOO}  ", "generic", env={"FOO": "k"}) == "k"


def test_missing_secret_is_a_value_error():
    """`cli.py` only catches ValueError/FileNotFoundError; anything else is a traceback."""
    assert issubclass(MissingSecret, ValueError)


def test_secret_env_names_covers_the_provider_convention():
    assert secret_env_names({"m": {"provider": "deepseek"}}) == {"DEEPSEEK_API_KEY"}


def test_secret_env_names_covers_a_custom_reference():
    """A ${MY_GATEWAY_KEY} reference is consumed too, and must be stripped."""
    names = secret_env_names({"m": {"api_key": "${MY_GATEWAY_KEY}"}})

    assert "MY_GATEWAY_KEY" in names


def test_secret_env_names_ignores_a_literal_key():
    assert secret_env_names({"m": {"api_key": "sk-literal"}}) == set()


def test_secret_env_names_survives_a_malformed_entry():
    """A config being wrong must not crash the security layer that reads it."""
    assert secret_env_names({"m": "not a mapping"}) == set()
    assert secret_env_names({}) == set()


def test_secret_env_names_does_not_raise_on_an_unknown_provider():
    """Strict validation belongs in `_coerce_model`, which knows the model name."""
    assert secret_env_names({"m": {"provider": "deapsek"}}) == set()
