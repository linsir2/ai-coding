"""Where a model's API key comes from (M-H2).

The order is ``${VAR}`` → literal → the provider's conventional variable.

That middle step is the one that matters: the environment fills an **absence**,
it never overrules a key the operator wrote out.  The alternative ("env wins")
looks more 12-factor, but it means a CI machine with ``DEEPSEEK_API_KEY``
exported silently sends someone else's account's key because a config file
happened to contain a literal — and it makes a test's result depend on the
machine it runs on.

Two consequences, both deliberate:

* a ``${VAR}`` reference that is missing **or empty** is a hard error naming the
  variable.  Silently resolving to ``""`` used to surface as
  ``The api_key client option must be set`` at SDK construction, which does not
  tell anyone which variable to export.
* ``MissingSecret`` is a ``ValueError``, because ``cli.py`` catches that and
  prints one line; anything else would be a traceback.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from typing import Any

from ai_coding.providers import UnknownProvider, api_key_env, provider_id_for

__all__ = ["MissingSecret", "env_reference", "resolve_api_key", "secret_env_names"]

#: A whole-value reference.  Not a template: ``prefix-${VAR}`` is a literal,
#: because a half-expanded secret is more likely a typo than an intent.
_REF = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")


def env_reference(raw: str) -> str | None:
    """The variable a ``${VAR}`` value names, or ``None`` for a literal.

    One spelling of the rule, because two consumers need it: resolving the key
    and deciding whether a *written* value is a reference (the sidecar keeps it
    as written rather than storing the plaintext).
    """
    match = _REF.match(str(raw or "").strip())
    return match.group(1) if match is not None else None


class MissingSecret(ValueError):
    """The configuration names a secret that is not available."""


def resolve_api_key(
    raw_api_key: str,
    provider_id: str,
    *,
    env: Mapping[str, str] | None = None,
) -> str:
    """The usable key for this model, or ``""`` when nothing named one."""
    environ = os.environ if env is None else env
    raw = str(raw_api_key or "").strip()

    name = env_reference(raw)
    if name is not None:
        if not environ.get(name):
            raise MissingSecret(
                f"api_key references ${{{name}}}, which is not set; export {name}"
            )
        return environ[name]

    if raw:
        # A key that was written out is never overruled by the environment.
        return raw

    name = api_key_env(provider_id)
    if name and environ.get(name):
        return environ[name]
    return ""


def secret_env_names(models_doc: Mapping[str, Any]) -> frozenset[str]:
    """Every environment variable this model table would consume.

    The process sandbox strips these from the children it spawns, so an agent
    cannot read the key back with ``echo $DEEPSEEK_API_KEY``.  It has to be
    computed from the **raw document**: once ``resolve_api_key`` has run, the
    name of a ``${VAR}`` reference is gone, and ``ModelConfig`` deliberately does
    not carry it (a field with no consumer is how ``streaming`` happened).

    Derived from the table rather than from what was actually read, so a variable
    for a provider configured but not used this run is stripped too.  Erring
    towards stripping is the safe direction.
    """
    names: set[str] = set()
    if not isinstance(models_doc, Mapping):
        return frozenset()
    for entry in models_doc.values():
        if not isinstance(entry, Mapping):
            continue
        try:
            provider_id = provider_id_for(
                str(entry.get("base_url") or entry.get("baseURL") or ""),
                explicit=str(entry.get("provider") or "").strip(),
            )
        except UnknownProvider:
            # Strict validation belongs in `_coerce_model`, which is the only
            # place that knows which model the entry is -- raising here would
            # report a typo with no name attached, and would make this "never
            # crash on a bad config" helper crash on one.
            continue
        conventional = api_key_env(provider_id)
        if conventional:
            names.add(conventional)
        reference = env_reference(str(entry.get("api_key") or ""))
        if reference is not None:
            names.add(reference)
    return frozenset(names)
