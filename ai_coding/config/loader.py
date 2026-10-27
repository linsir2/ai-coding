"""YAML loading with camelCase -> snake_case normalization and default fallback."""

from __future__ import annotations

import copy
import logging
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from ai_coding.paths import ancestors_up_to_home

from .models import AppConfig, MCPConfig, MCPServerConfig, _coerce_model

_log = logging.getLogger(__name__)


#: Top-level keys whose value can make the harness *execute* something or send
#: it somewhere.  A project's config is not trusted to grant that: the operator
#: who runs ``ai-coding`` in a directory trusts its files, not its config, and
#: ``config.example.yaml`` ships in the repository.  Only the user layer -- and
#: the operator's explicit opt-in, see ``_project_execution_allowed`` -- may set
#: these.
#:
#: Deliberately narrow.  ``models`` and ``tools`` stay trusted because their
#: dangerous members (``base_url``, ``extra_workspace_roots``) are a different
#: problem with a different fix, and lumping them in would break every project's
#: model selection and tool list at once.
#:
#: ``process_sandbox`` (M-D3) is here because it is the *only* switch that turns
#: the OS-level write boundary off: a repository's tracked config must not be
#: able to make every bash/job in the workspace run unsandboxed.
#:
#: M-D2's read boundary (``read_deny_paths``) and AF_UNIX layer
#: (``af_unix_deny_paths`` / ``block_af_unix``) live **inside** ``process_sandbox``
#: (deliberately, review P2-3): they are covered by this one key, so a project
#: layer cannot widen the read denylist or turn off the AF_UNIX masks either.
#: Adding them as top-level ``EXECUTION_KEYS`` would be a no-op.
EXECUTION_KEYS: frozenset[str] = frozenset({"hooks", "mcp", "process_sandbox"})

#: Members that decide *where* a request goes or *which secret* it carries.
#: Same reasoning as ``EXECUTION_KEYS`` -- the operator trusts the directory as a
#: workspace, not its config -- one level finer: the block stays (a project may
#: still tune how the agent behaves), only the destination and the credential are
#: withheld.
#:
#: ``models`` is the one block whose *entries* are also filtered: see
#: ``_filter_project_document``.
PROJECT_FORBIDDEN_MEMBERS: dict[str, frozenset[str]] = {
    "ai": frozenset({"websearch_api_key"}),
    "tracing": frozenset({"dir"}),
}
#: One level deeper: ``memory.embeddings.cache_path``.
PROJECT_FORBIDDEN_NESTED: dict[str, dict[str, frozenset[str]]] = {
    "memory": {"embeddings": frozenset({"cache_path"})},
}
#: ``models`` members that decide the destination or the credential.  Applied per
#: entry, after the entry itself has survived the "must be user-defined" rule.
#:
#: ``provider`` is here because it selects **both**: it supplies a default
#: ``base_url`` and it picks the environment variable the key is read from
#: (``ai_coding/providers.py``).  A repository that could set it could make the
#: agent send an ``OPENAI_API_KEY`` the operator meant for something else.
MODEL_DESTINATION_MEMBERS: frozenset[str] = frozenset(
    {"base_url", "api_key", "provider"}
)

#: Top-level blocks ``AppConfig`` holds as mappings.  A project may not replace
#: one with a scalar even when the operator never configured it: the schema wants
#: a mapping there, so a scalar is a validation error the repository would be
#: inflicting on the operator.
MAPPING_BLOCKS: frozenset[str] = frozenset(
    {"models", "tools", "ai", "memory", "mcp", "tracing", "guard", "hooks"}
)
#: Mapping members one level down, by the same schema.
NESTED_MAPPING_BLOCKS: dict[str, frozenset[str]] = {
    "tools": frozenset({"bash", "read", "write", "edit", "glob", "grep", "ask_user"}),
    "memory": frozenset({"embeddings"}),
}

#: The one channel a repository cannot touch, which is what makes it a permission
#: rather than a config value (same reasoning as ``core/static_check.py``).
TRUST_PROJECT_ENV = "AI_CODING_TRUST_PROJECT"


def _project_execution_allowed() -> bool:
    """Did the operator explicitly trust this project's execution settings?"""
    return os.environ.get(TRUST_PROJECT_ENV) == "1"


def _mcp_doc(doc: dict[str, Any], path: str | Path | None) -> dict[str, Any]:
    """The ``mcp:`` section with any sidecar servers merged in (M5).

    The main file's servers come first, then the ones the wizard added, so a
    name collision is visible rather than silently resolved: the manager
    refuses to adopt a duplicate, and the row says which server won.
    """
    section = dict(doc.get("mcp") or {})
    sidecar = ConfigLoader.mcp_sidecar(path)
    if sidecar is None or not sidecar.is_file():
        return section
    try:
        extra = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or []
    except yaml.YAMLError:
        _log.warning("mcp.local.yaml is not valid YAML; ignoring it")
        return section
    if not isinstance(extra, list):
        return section
    servers = list(section.get("servers") or [])
    servers.extend(entry for entry in extra if isinstance(entry, dict))
    section["servers"] = servers
    return section


def _to_snake(key: str) -> str:
    # lowercamel/CamelCase -> snake_case, keeping acronyms as a unit (baseURL -> base_url).
    return re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", key
    ).lower()


#: Maps whose keys are data rather than field names: they must reach the model
#: verbatim.  An environment variable name is case-sensitive, so normalizing
#: ``TOKEN`` into ``token`` would hand the child process the wrong variable.
_OPAQUE_MAPS = frozenset({"environment"})


def _normalize(value: Any, parent_key: str | None = None) -> Any:
    if isinstance(value, dict):
        if parent_key in _OPAQUE_MAPS:
            return dict(value)
        return {_to_snake(k): _normalize(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_normalize(v) for v in value]
    return value


def _strip_scalar_overwrites(
    node: dict[str, Any],
    user_node: dict[str, Any],
    dropped: list[str],
    prefix: str = "",
) -> None:
    """Drop a project value that would turn a mapping into a non-mapping.

    At any depth the operator configured, plus the schema's own top-level mapping
    blocks.  ``models:`` (YAML null) used to reach ``_overlay``'s else branch and
    overwrite the operator's dict with ``None``; ``models: {gpt-x: null}`` does
    the same one level down, and ``tools: {bash: null}`` would fail validation
    outright -- in every case the repository, not the operator, decides whether
    the app starts.  ``NESTED_MAPPING_BLOCKS`` covers the one level the schema
    names explicitly.
    """
    for key, value in list(node.items()):
        name = f"{prefix}{key}"
        user_value = user_node.get(key) if isinstance(user_node, dict) else None
        if isinstance(value, dict):
            if isinstance(user_value, dict):
                _strip_scalar_overwrites(value, user_value, dropped, f"{name}.")
            continue
        if isinstance(user_value, dict) or (not prefix and key in MAPPING_BLOCKS):
            node.pop(key)
            dropped.append(name)


def _filter_project_document(
    document: dict[str, Any],
    user_document: dict[str, Any],
    *,
    workspace: Path,
) -> tuple[dict[str, Any], list[str]]:
    """Strip from a project document the members a repository may not grant.

    Returns the filtered document and the dotted names dropped, so the caller can
    say so once instead of silently changing behaviour.

    The rules, in the order they are applied:

    * a top-level key the operator holds as a mapping may not be replaced by a
      scalar (``models:`` with YAML null used to wipe every model);
    * a ``models`` entry the operator did not define is dropped whole -- it can
      never carry an endpoint, so it is a hollow shell -- and a surviving entry
      keeps only its behaviour knobs, losing ``base_url``/``api_key``;
    * ``default_model`` is dropped only when it points at a model the project
      itself introduced (the one case that would leave a dangling reference);
    * the members in ``PROJECT_FORBIDDEN_MEMBERS`` / ``PROJECT_FORBIDDEN_NESTED``
      and ``tools.extra_workspace_roots`` are withheld or narrowed.
    """
    filtered = copy.deepcopy(document)
    dropped: list[str] = []

    # Rule 0: a nearer layer may narrow, never remove.  ``models:`` (YAML null)
    # reaches ``_overlay``'s else branch and overwrites the operator's dict with
    # ``None``; the same shape does it one level down, and the repository then
    # decides whether the operator's app starts at all.
    _strip_scalar_overwrites(filtered, user_document, dropped)
    for block, members in NESTED_MAPPING_BLOCKS.items():
        outer = filtered.get(block)
        if not isinstance(outer, dict):
            continue
        for member in members:
            if member in outer and not isinstance(outer[member], dict):
                outer.pop(member)
                dropped.append(f"{block}.{member}")

    # Rules 1-3: models.
    user_models = user_document.get("models")
    user_models = user_models if isinstance(user_models, dict) else {}
    project_models = filtered.get("models")
    if isinstance(project_models, dict):
        kept_models: dict[str, Any] = {}
        introduced: set[str] = set()
        for name, entry in project_models.items():
            if name not in user_models:
                introduced.add(name)
                dropped.append(f"models.{name}")
                continue
            if isinstance(entry, dict):
                for member in MODEL_DESTINATION_MEMBERS:
                    if member in entry:
                        dropped.append(f"models.{name}.{member}")
                entry = {
                    k: v for k, v in entry.items() if k not in MODEL_DESTINATION_MEMBERS
                }
            kept_models[name] = entry
        filtered["models"] = kept_models
        default_model = filtered.get("default_model")
        if isinstance(default_model, str) and default_model in introduced:
            filtered.pop("default_model", None)
            dropped.append("default_model")

    # The flat member lists: a secret and a path that would leave the workspace.
    for block, members in PROJECT_FORBIDDEN_MEMBERS.items():
        value = filtered.get(block)
        if not isinstance(value, dict):
            continue
        for member in members:
            if member in value:
                value.pop(member)
                dropped.append(f"{block}.{member}")

    # One level deeper, for the embedding cache path.
    for block, sub_blocks in PROJECT_FORBIDDEN_NESTED.items():
        outer = filtered.get(block)
        if not isinstance(outer, dict):
            continue
        for sub, members in sub_blocks.items():
            inner = outer.get(sub)
            if not isinstance(inner, dict):
                continue
            for member in members:
                if member in inner:
                    inner.pop(member)
                    dropped.append(f"{block}.{sub}.{member}")

    # The grant: a project may only narrow the operator's roots, never widen.
    tools = filtered.get("tools")
    if isinstance(tools, dict) and "extra_workspace_roots" in tools:
        from ai_coding.tools.builder import expand_root_patterns

        user_tools = user_document.get("tools")
        user_patterns = (
            user_tools.get("extra_workspace_roots") if isinstance(user_tools, dict) else None
        )
        project_patterns = tools.get("extra_workspace_roots")
        # Expanded-empty is the test, not the raw list: ``[""]`` and a pattern
        # that matches nothing would otherwise clear the operator's grant.
        if not expand_root_patterns(project_patterns, base=workspace):
            tools.pop("extra_workspace_roots")
        else:
            tools["extra_workspace_roots"] = _narrow_roots(
                project_patterns, user_patterns, workspace=workspace
            )

    return filtered, dropped


def _narrow_roots(
    project_patterns: Any, user_patterns: Any, *, workspace: Path
) -> list[str]:
    """Keep the operator's roots that the project's declaration covers.

    "A project may narrow the operator's grant, never widen it."  Computed on the
    expanded directories, because two pattern sets have no intersection of their
    own.  An empty project declaration is handled by the caller removing the key
    outright, so it never reaches here.
    """
    from ai_coding.tools.builder import expand_root_patterns

    project_dirs = expand_root_patterns(project_patterns, base=workspace)
    user_dirs = expand_root_patterns(user_patterns, base=workspace)
    return [
        str(directory)
        for directory in user_dirs
        if any(directory.is_relative_to(project) for project in project_dirs)
    ]


class ConfigLoader:
    """Load ``AppConfig`` and ``MCPConfig`` from a YAML file (or built-in defaults)."""

    #: Probed in order when no explicit path is given. ``config.local.yaml`` is
    #: gitignored and holds the API key, so it must win over the example file.
    DEFAULT_NAMES: tuple[str, ...] = (
        "config.local.yaml",
        "config.yaml",
        "config.example.yaml",
    )

    #: The file that only documents itself. It is a fallback, never a winner:
    #: it ships in the repo, so directory proximity alone used to let it shadow
    #: an operator's `config.local.yaml` one level up — the file that holds the
    #: API key.
    EXAMPLE_NAME = "config.example.yaml"

    @staticmethod
    def _ancestors(base: Path) -> list[Path]:
        """``base`` then its parents, stopping short of the user's home
        directory.

        Shared with the project-instruction loader: both walk up from the
        workspace, and both would otherwise let a file at ``$HOME`` answer for
        every project. One helper, so the boundary cannot drift between them.
        """
        return ancestors_up_to_home(base)

    @staticmethod
    def discover(start: str | Path | None = None) -> Path | None:
        """Find a config file by walking up from ``start``.

        Bare ``ai-coding`` in a project directory should just work, so the lookup
        climbs parent directories: a repo root config applies to any
        subdirectory you launch from.

        Two ordering rules, both of which used to be wrong:

        * the search is anchored on the **workspace**, not the process CWD.
          ``ai-coding -w /projects/B`` from anywhere must find B's config, or
          the run starts on empty defaults and cannot even build an engine.
        * a **real** config beats an example one wherever each sits. Directory
          proximity is not evidence of intent; ``config.example.yaml`` is
          documentation and is consulted only if nothing real exists anywhere
          on the way up.
        """
        base = Path(start).expanduser().resolve() if start else Path.cwd()
        if base.is_file():
            base = base.parent
        directories = ConfigLoader._ancestors(base)
        real = [name for name in ConfigLoader.DEFAULT_NAMES if name != ConfigLoader.EXAMPLE_NAME]
        for name in real:
            for directory in directories:
                candidate = directory / name
                if candidate.is_file():
                    return candidate
        for directory in directories:
            candidate = directory / ConfigLoader.EXAMPLE_NAME
            if candidate.is_file():
                return candidate
        return None

    @staticmethod
    def _read_doc(path: str | Path | None) -> dict[str, Any]:
        if path is None:
            path = ConfigLoader.discover()
            if path is None:
                return {}
        file_path = Path(path)
        if not file_path.is_file():
            raise FileNotFoundError(f"config file not found: {file_path}")
        with file_path.open("r", encoding="utf-8") as fh:
            doc = yaml.safe_load(fh) or {}
        return doc if isinstance(doc, dict) else {}

    @staticmethod
    def load_app(
        path: str | Path | None = None, *, env: Mapping[str, str] | None = None
    ) -> AppConfig:
        return ConfigLoader.app_from_doc(
            _normalize(ConfigLoader._read_doc(path)), path, env=env
        )

    @staticmethod
    def app_from_doc(
        doc: dict[str, Any],
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> AppConfig:
        """Validate an already-merged document.

        Split out from :meth:`load_app` because layering produces one document
        from several files, and re-reading any single one of them to validate it
        would silently drop the rest.  ``env`` is the secret source handed to
        every model entry; ``None`` means the real environment.
        """
        models_raw = doc.get("models") or {}
        coerced = {
            name: _coerce_model(entry, env=env) if isinstance(entry, dict) else entry
            for name, entry in models_raw.items()
        }
        doc["models"] = coerced
        # The app config carries its own ``mcp:`` root — that is the one
        # ``build_world`` hands the manager — so the sidecar has to be merged
        # into this document as well, or a wizard-added server is invisible on
        # the next start even though ``load_mcp`` can see it.
        doc["mcp"] = _mcp_doc(doc, path)
        return AppConfig.model_validate(doc)

    # ---- layered config (M9) -----------------------------------------------

    @staticmethod
    def _layers_for(
        paths: Any, explicit: str | Path | None
    ) -> list[tuple[str, Path | None]]:
        """``[(role, path)]`` from broadest to narrowest.

        An explicit ``--path`` is the only layer: it means that file, which is
        what it meant before layering existed.
        """
        if explicit is not None:
            return [("explicit", Path(explicit))]
        layers: list[tuple[str, Path | None]] = [("user", paths.user_config)]
        project = ConfigLoader.discover(paths.workspace)
        if project is not None:
            layers.append(("project", project))
        return layers

    @staticmethod
    def _read_layer(path: Path | None) -> dict[str, Any]:
        if path is None or not Path(path).is_file():
            return {}
        try:
            return ConfigLoader._read_doc(path)
        except (OSError, yaml.YAMLError):
            # A malformed personal file must not make every project
            # unstartable; a malformed project file must not lose the personal
            # settings either. Either way it is skipped, and the next layer in
            # is the one that answers.
            _log.warning("config layer ignored: %s is unreadable", path)
            return {}

    @staticmethod
    def _overlay(base: dict[str, Any], top: dict[str, Any]) -> dict[str, Any]:
        """Deep merge, ``top`` wins per key. Lists replace -- except ``servers``.

        Lists replace on purpose: a list is a declaration of what exists, not an
        additive layer, and concatenating it would let a project silently
        re-enable something the global layer turned off.  ``servers`` is the
        deliberate exception: a project adds MCP servers to the user's list
        rather than replacing it, and the name collision is surfaced by the
        manager instead of being resolved here.

        Note the trust boundary sits one level up (``load_layers_report``): the
        project layer's ``hooks``/``mcp`` are dropped before they reach this
        function, so "a project adds servers" only holds for a trusted project.
        """
        merged = dict(base)
        for key, value in top.items():
            current = merged.get(key)
            if isinstance(current, dict) and isinstance(value, dict):
                merged[key] = ConfigLoader._overlay(current, value)
            elif key == "servers" and isinstance(current, list) and isinstance(value, list):
                merged[key] = current + value
            else:
                merged[key] = value
        return merged

    @staticmethod
    def load_layers_report(
        paths: Any,
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> LayeredConfig:
        """Load both layers and record which file answered each top-level key.

        The record exists because a merged config cannot answer "why did this
        project pick that model?" — and a run that behaves differently in two
        directories is exactly the bug this layering was added to fix.
        """
        merged: dict[str, Any] = {}
        sources: dict[str, str] = {}
        chosen: Path | None = None
        trusted = _project_execution_allowed()
        user_document: dict[str, Any] = {}
        for role, layer in ConfigLoader._layers_for(paths, path):
            # ``chosen`` anchors the ``mcp.local.yaml`` sidecar (``_mcp_doc``),
            # so in layered mode it must be the *user* layer -- even when that
            # file does not exist.  Leaving it ``None`` is not neutral:
            # ``mcp_sidecar(None)`` falls back to ``discover()``, which anchors on
            # ``Path.cwd()``, i.e. the project directory.  So the anchor has to
            # be set before the "nothing to read" skip below, and only an
            # explicit ``--path`` or the operator's opt-in may point it at a
            # project file.
            if role != "project" or trusted:
                if layer is not None:
                    chosen = layer
            document = ConfigLoader._read_layer(layer)
            if not document:
                continue
            # Normalize FIRST: ``_to_snake`` turns ``Hooks``/``MCP``/``Mcp`` into
            # ``hooks``/``mcp``, so filtering the raw document would leave a
            # one-word bypass.  Normalizing here also keeps ``sources`` and the
            # merged config describing the same keys.
            document = _normalize(document)
            if role == "user":
                user_document = document
            # A project layer may not grant execution, endpoints, credentials or
            # paths outside the workspace.  They are dropped here, before they
            # can be merged over the user's, and said so once -- a silent drop
            # would read as "the feature is broken".
            if role == "project" and not trusted:
                document, dropped = _filter_project_document(
                    document, user_document, workspace=paths.workspace
                )
                execution_dropped = [k for k in document if k in EXECUTION_KEYS]
                if execution_dropped:
                    document = {
                        k: v for k, v in document.items() if k not in EXECUTION_KEYS
                    }
                if dropped or execution_dropped:
                    _log.warning(
                        "project config ignored for %s (a repository cannot grant "
                        "execution, endpoints, credentials or paths outside the "
                        "workspace; set %s=1 to allow it). A dropped api_key is "
                        "still readable by the agent in that file -- move models "
                        "and keys to the user layer: `ai-coding config path`",
                        ", ".join(sorted([*execution_dropped, *dropped])),
                        TRUST_PROJECT_ENV,
                    )
            for key in document:
                sources[key] = str(layer)
            merged = ConfigLoader._overlay(merged, document)
        # Computed **before** `app_from_doc`: it replaces `merged["models"]` with
        # the coerced entries in place, and after that the name of a `${VAR}`
        # reference is gone (M-H2).
        from ai_coding.config.secrets import secret_env_names

        names = secret_env_names(merged.get("models") or {})
        return LayeredConfig(
            config=ConfigLoader.app_from_doc(merged, chosen, env=env)
            if merged
            else AppConfig(),
            sources=sources,
            layers=[str(layer) for _role, layer in ConfigLoader._layers_for(paths, path)],
            secret_env_names=names,
        )

    @staticmethod
    def load_layers(
        paths: Any,
        path: str | Path | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> AppConfig:
        """``AppConfig`` from the user layer with the project layer on top."""
        return ConfigLoader.load_layers_report(paths, path, env=env).config

    @staticmethod
    def load_app_from(start: str | Path | None = None) -> AppConfig:
        """``load_app`` against a specific search root, ignoring the cwd."""
        found = ConfigLoader.discover(start)
        return ConfigLoader.load_app(found) if found is not None else AppConfig()

    @staticmethod
    def load_mcp(path: str | Path | None = None) -> MCPConfig:
        doc = _normalize(ConfigLoader._read_doc(path))
        return MCPConfig.model_validate(_mcp_doc(doc, path))

    @staticmethod
    def mcp_sidecar(path: str | Path | None = None) -> Path | None:
        """Where runtime-added MCP servers live (``mcp.local.yaml``, M5).

        A *sibling* of the config file, never the config file itself: that one
        holds comments and the API key, so rewriting it from code would destroy
        both.  Gitignored for the same reason ``config.local.yaml`` is.
        """
        base = Path(path) if path is not None else ConfigLoader.discover()
        if base is None or not base.is_file():
            return None
        return base.parent / "mcp.local.yaml"

    @staticmethod
    def append_mcp_server(path: str | Path | None, server: MCPServerConfig) -> Path:
        """Persist one runtime-added server to the sidecar (M5).

        Write-merge-append over a file this code owns, so the operator's own
        config is never touched.  Returns the file written.
        """
        sidecar = ConfigLoader.mcp_sidecar(path)
        if sidecar is None:
            raise FileNotFoundError("no config file to write mcp.local.yaml beside")
        existing: list[Any] = []
        if sidecar.is_file():
            loaded = yaml.safe_load(sidecar.read_text(encoding="utf-8")) or []
            if isinstance(loaded, list):
                existing = loaded
        existing.append(server.model_dump(mode="json", exclude_none=True))
        sidecar.write_text(
            yaml.safe_dump(existing, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        # The sidecar can carry environment variables, which are secrets.
        os.chmod(sidecar, 0o600)
        return sidecar


class LayeredConfig:
    """A merged ``AppConfig`` plus where each top-level key came from."""

    __slots__ = ("config", "sources", "layers", "secret_env_names")

    def __init__(
        self,
        config: AppConfig,
        sources: dict[str, str],
        layers: list[str],
        secret_env_names: frozenset[str] = frozenset(),
    ) -> None:
        self.config = config
        self.sources = sources
        self.layers = layers
        #: Environment variables this configuration consumes as secrets, so the
        #: process sandbox can strip them from the children it spawns (M-H2).
        #: Carried here rather than recomputed later because once the key is
        #: resolved, the name of a ``${VAR}`` reference is gone.
        self.secret_env_names = secret_env_names


class ConfigManager:
    """Holds the two decoupled roots, loaded together from a single YAML file."""

    def __init__(self) -> None:
        self.app: AppConfig = AppConfig()
        self.mcp: MCPConfig = MCPConfig()

    def initialize(
        self, path: str | Path | None = None, *, search_from: str | Path | None = None
    ) -> None:
        """``path`` is an explicit ``--path``; ``search_from`` is where to look
        when there is none.

        They are separate because they were not: discovery used to fall back to
        ``Path.cwd()``, so ``ai-coding -w /projects/B`` started from anywhere
        else read *that* directory's config and then built an engine on empty
        defaults.
        """
        found = ConfigLoader.discover(search_from) if path is None else Path(path)
        self.app = ConfigLoader.load_app(found)
        self.mcp = ConfigLoader.load_mcp(found)
