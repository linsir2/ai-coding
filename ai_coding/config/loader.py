"""YAML loading with camelCase -> snake_case normalization and default fallback."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
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

    @staticmethod
    def discover(start: str | Path | None = None) -> Path | None:
        """Find a config file by walking up from ``start`` (default: cwd).

        Bare ``ai-coding`` in a project directory should just work, so the
        lookup climbs parent directories: a repo root config applies to any
        subdirectory you launch from.
        """
        base = Path(start).expanduser().resolve() if start else Path.cwd()
        if base.is_file():
            base = base.parent
        for directory in (base, *base.parents):
            for name in ConfigLoader.DEFAULT_NAMES:
                candidate = directory / name
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
        doc = _normalize(ConfigLoader._read_doc(path))
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


class ConfigManager:
    """Holds the two decoupled roots, loaded together from a single YAML file."""

    def __init__(self) -> None:
        self.app: AppConfig = AppConfig()
        self.mcp: MCPConfig = MCPConfig()

    def initialize(self, path: str | Path | None = None) -> None:
        self.app = ConfigLoader.load_app(path)
        self.mcp = ConfigLoader.load_mcp(path)
