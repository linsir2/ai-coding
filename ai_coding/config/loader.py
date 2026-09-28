"""YAML loading with camelCase -> snake_case normalization and default fallback."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .models import AppConfig, MCPConfig, _coerce_model


def _to_snake(key: str) -> str:
    # lowercamel/CamelCase -> snake_case, keeping acronyms as a unit (baseURL -> base_url).
    return re.sub(
        r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])", "_", key
    ).lower()


def _normalize(value: Any) -> Any:
    if isinstance(value, dict):
        return {_to_snake(k): _normalize(v) for k, v in value.items()}
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
        return AppConfig.model_validate(doc)

    @staticmethod
    def load_app_from(start: str | Path | None = None) -> AppConfig:
        """``load_app`` against a specific search root, ignoring the cwd."""
        found = ConfigLoader.discover(start)
        return ConfigLoader.load_app(found) if found is not None else AppConfig()

    @staticmethod
    def load_mcp(path: str | Path | None = None) -> MCPConfig:
        doc = _normalize(ConfigLoader._read_doc(path))
        return MCPConfig.model_validate(doc.get("mcp") or {})


class ConfigManager:
    """Holds the two decoupled roots, loaded together from a single YAML file."""

    def __init__(self) -> None:
        self.app: AppConfig = AppConfig()
        self.mcp: MCPConfig = MCPConfig()

    def initialize(self, path: str | Path | None = None) -> None:
        self.app = ConfigLoader.load_app(path)
        self.mcp = ConfigLoader.load_mcp(path)
