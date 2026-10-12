"""YAML loading with camelCase -> snake_case normalization and default fallback."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from ai_coding.paths import ancestors_up_to_home
from typing import Any

import yaml

from .models import AppConfig, MCPConfig, MCPServerConfig, _coerce_model

_log = logging.getLogger(__name__)


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
    def load_app(path: str | Path | None = None) -> AppConfig:
        return ConfigLoader.app_from_doc(
            _normalize(ConfigLoader._read_doc(path)), path
        )

    @staticmethod
    def app_from_doc(doc: dict[str, Any], path: str | Path | None = None) -> AppConfig:
        """Validate an already-merged document.

        Split out from :meth:`load_app` because layering produces one document
        from several files, and re-reading any single one of them to validate it
        would silently drop the rest.
        """
        models_raw = doc.get("models") or {}
        coerced = {
            name: _coerce_model(entry) if isinstance(entry, dict) else entry
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
        """Deep merge, ``top`` wins per key. Lists replace rather than extend.

        Lists replace on purpose: ``mcp.servers`` and ``tools.enabled`` are
        declarations of what exists, not additive layers, and concatenating
        them would let a project's ``enabled: [bash]`` silently re-enable
        everything the global layer turned off.
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
        paths: Any, path: str | Path | None = None
    ) -> LayeredConfig:
        """Load both layers and record which file answered each top-level key.

        The record exists because a merged config cannot answer "why did this
        project pick that model?" — and a run that behaves differently in two
        directories is exactly the bug this layering was added to fix.
        """
        merged: dict[str, Any] = {}
        sources: dict[str, str] = {}
        chosen: Path | None = None
        for _role, layer in ConfigLoader._layers_for(paths, path):
            document = ConfigLoader._read_layer(layer)
            if not document:
                continue
            if chosen is None or layer is not None:
                chosen = layer
            for key in document:
                sources[key] = str(layer)
            merged = ConfigLoader._overlay(merged, _normalize(document))
        return LayeredConfig(
            config=ConfigLoader.app_from_doc(merged, chosen) if merged else AppConfig(),
            sources=sources,
            layers=[str(layer) for _role, layer in ConfigLoader._layers_for(paths, path)],
        )

    @staticmethod
    def load_layers(paths: Any, path: str | Path | None = None) -> AppConfig:
        """``AppConfig`` from the user layer with the project layer on top."""
        return ConfigLoader.load_layers_report(paths, path).config

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

    __slots__ = ("config", "sources", "layers")

    def __init__(
        self,
        config: AppConfig,
        sources: dict[str, str],
        layers: list[str],
    ) -> None:
        self.config = config
        self.sources = sources
        self.layers = layers


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
