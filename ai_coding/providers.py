"""Which endpoint is this, and what does it call its key? (M-H2)

Most provider differences are already handled without knowing the provider:
``domain/usage.py::cache_from_usage`` tries five usage shapes in turn, and
``ai/openai_engine.py`` tries ``reasoning_content`` then ``reasoning``, matches
context-overflow text, omits an empty ``tools`` list, and probes ``/embeddings``
with a 404.  That is deliberate and stays as it is.

Three things cannot be guessed that way, and each needs to know *which* provider
this is:

* the conventional environment variable that carries the key
  (``DEEPSEEK_API_KEY``), so an absent ``api_key`` can still resolve;
* a default ``base_url``, so ``provider: deepseek`` is enough and there is one
  fewer field to get wrong;
* a stable id, which is what a config names.

So this is a table of three fields, not a LiteLLM.  Adding a provider is one
line; if a field ever loses its consumer, delete it.

**Zero internal imports on purpose.**  ``config/`` and ``server/`` both need
this, and ``ai/factory.py`` already imports ``config.models`` — a module under
``ai/`` would close that cycle.  Top-level leaf modules are the established shape
here (``paths.py``, ``shell_parse.py``, ``attachments.py``).
"""

from __future__ import annotations

import dataclasses
from urllib.parse import urlparse

__all__ = [
    "PROVIDERS",
    "Provider",
    "UnknownProvider",
    "api_key_env",
    "default_base_url",
    "host_port",
    "provider_id_for",
]

#: The id used when nothing matches: an OpenAI-compatible endpoint we know
#: nothing else about.  It carries no defaults, so it never guesses.
GENERIC = "generic"


class UnknownProvider(ValueError):
    """A config named a provider that is not in the table.

    ``ValueError`` on purpose: ``cli.py`` catches it and prints one line, and a
    typo must be said out loud rather than silently becoming ``generic`` (which
    would pick no environment variable at all).
    """


@dataclasses.dataclass(frozen=True)
class Provider:
    """One endpoint family: where it lives by default, and what it calls its key.

    There is no ``id`` field: the table key *is* the id, and a field that only
    restates its own key is a field with no consumer.
    """

    #: Empty for ``generic``: there is no default, so ``base_url`` stays required.
    default_base_url: str
    #: The conventional environment variable.  Empty when a key is not needed
    #: (``ollama``) or unknown (``generic``).
    api_key_env: str


PROVIDERS: dict[str, Provider] = {
    "openai": Provider("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "deepseek": Provider("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY"),
    "qwen": Provider(
        "https://dashscope.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"
    ),
    "ollama": Provider("http://localhost:11434/v1", ""),
    GENERIC: Provider("", ""),
}


def host_port(base_url: str) -> tuple[str, int | None]:
    """``(host, port)`` for an endpoint, with a default port filled in.

    The only URL parsing in the tree that decides an identity, so there is one
    place to get it wrong.  Two shapes ``urlparse`` gets wrong on its own:

    * a bare ``api.example.com/v1`` would be read as a *path*, so the ``://``
      test (not ``//``) decides whether a scheme is present;
    * with no explicit port the scheme's default is used, and a bare host is
      assumed to be ``https`` -- every endpoint in the table is.
    """
    raw = (base_url or "").strip()
    if not raw:
        return "", None
    parsed = urlparse(raw if "://" in raw else "//" + raw)
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 80 if parsed.scheme.lower() == "http" else 443
    return host, port


def provider_id_for(base_url: str, *, explicit: str = "") -> str:
    """The provider this endpoint belongs to.

    An explicit name wins.  Otherwise the endpoint is matched on **host and
    port** against each provider's default: host alone would claim every local
    server for ``ollama`` (``localhost:8000`` is not ``localhost:11434``).
    """
    name = (explicit or "").strip().lower()
    if name:
        if name not in PROVIDERS:
            raise UnknownProvider(
                f"unknown provider {name!r}; known providers: {sorted(PROVIDERS)}"
            )
        return name
    key = host_port(base_url)
    if key[0]:
        for provider_id, provider in PROVIDERS.items():
            if provider.default_base_url and host_port(provider.default_base_url) == key:
                return provider_id
    return GENERIC


def default_base_url(provider_id: str) -> str:
    provider = PROVIDERS.get(provider_id)
    return provider.default_base_url if provider is not None else ""


def api_key_env(provider_id: str) -> str:
    provider = PROVIDERS.get(provider_id)
    return provider.api_key_env if provider is not None else ""
