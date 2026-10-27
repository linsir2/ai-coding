"""M-H2: the provider table — which endpoint is this, and what does it call its key.

`domain/usage.py` already normalises five usage shapes and `openai_engine` already
tries two reasoning fields, so most provider differences are handled without
knowing the provider at all.  Three things are not: the conventional env-var name
for the key, whether `base_url` can be omitted, and (indirectly) which entry of
the table a gateway is.  This table answers exactly those.
"""

from __future__ import annotations

import pytest

from ai_coding.providers import (
    PROVIDERS,
    UnknownProvider,
    api_key_env,
    default_base_url,
    host_port,
    provider_id_for,
)


def test_the_table_has_a_generic_fallback():
    """Anything we do not recognise still gets a row, with no defaults to guess."""
    assert "generic" in PROVIDERS
    assert PROVIDERS["generic"].api_key_env == ""
    assert PROVIDERS["generic"].default_base_url == ""


def test_host_port_fills_the_default_port():
    """A bare host is assumed https; an explicit `http` is not."""
    assert host_port("https://api.deepseek.com/v1") == ("api.deepseek.com", 443)
    assert host_port("api.deepseek.com/v1") == ("api.deepseek.com", 443)
    assert host_port("http://api.openai.com/v1") == ("api.openai.com", 80)
    assert host_port("http://localhost:11434/v1") == ("localhost", 11434)
    assert host_port("") == ("", None)


def test_host_port_survives_a_double_slash_in_the_path():
    """`//` used to be the "has a scheme" test, which broke this into host ''."""
    assert host_port("localhost:11434//v1") == ("localhost", 11434)


def test_a_bare_host_still_infers_the_provider():
    """No scheme is a common config shape; losing it would lose the env var too."""
    assert provider_id_for("api.deepseek.com/v1") == "deepseek"


def test_an_explicit_provider_wins():
    assert provider_id_for("https://api.openai.com/v1", explicit="deepseek") == "deepseek"


def test_an_unknown_explicit_provider_is_a_hard_error():
    """A typo must say so; silently falling back to generic would pick no env var."""
    with pytest.raises(UnknownProvider) as caught:
        provider_id_for("https://api.openai.com/v1", explicit="deapsek")

    message = str(caught.value)
    assert "deapsek" in message
    assert "deepseek" in message, "the message must list the valid ids"


def test_a_known_host_infers_the_provider():
    assert provider_id_for("https://api.deepseek.com/v1") == "deepseek"
    # The path is not part of the identity: a compatibility shim on the same
    # host is still the same provider.
    assert provider_id_for("https://api.deepseek.com/anthropic") == "deepseek"
    assert provider_id_for("https://api.openai.com/v1") == "openai"


def test_localhost_with_the_ollama_port_is_ollama():
    assert provider_id_for("http://localhost:11434/v1") == "ollama"


def test_localhost_with_another_port_is_not_ollama():
    """Host-only matching would claim every local server; the port is the tell."""
    assert provider_id_for("http://localhost:8000/v1") == "generic"


def test_an_unknown_host_falls_back_to_generic():
    assert provider_id_for("https://my-gateway.corp/v1") == "generic"
    assert provider_id_for("") == "generic"


def test_the_default_base_url_comes_from_the_table():
    assert default_base_url("deepseek") == "https://api.deepseek.com/v1"
    assert default_base_url("generic") == ""


def test_the_env_var_name_comes_from_the_table():
    assert api_key_env("deepseek") == "DEEPSEEK_API_KEY"
    assert api_key_env("openai") == "OPENAI_API_KEY"
    assert api_key_env("ollama") == "", "a local server needs no key"
