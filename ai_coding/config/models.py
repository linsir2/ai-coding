"""Pydantic configuration models with defaults aligned to the original Java values."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ai_coding.domain.model_config import ModelConfig


class ToolConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    max_file_size: int = 10_485_760  # 10 MB
    timeout_seconds: int = 30


class ToolsConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bash: ToolConfig = Field(default_factory=ToolConfig)
    read: ToolConfig = Field(default_factory=ToolConfig)
    write: ToolConfig = Field(default_factory=ToolConfig)
    edit: ToolConfig = Field(default_factory=ToolConfig)
    glob: ToolConfig = Field(default_factory=ToolConfig)


class AIConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    auto_process_tool_results: bool = True
    max_tool_iterations: int = 10
    max_concurrent_subagents: int = 3
    subagent_worktree_isolation: bool = True
    max_context_tokens: int = 48_000
    max_messages: int = 50
    snip_keep_head: int = 3
    snip_keep_tail: int = 20
    keep_recent_tool_results: int = 3
    per_result_persist_bytes: int = 30_000
    l4_keep_tail: int = 6


class MemoryConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    auto_extract: bool = True
    consolidate_threshold: int = 10
    max_index_entries: int = 200
    max_per_turn_injections: int = 5


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    models: dict[str, ModelConfig] = Field(default_factory=dict)
    default_model: str = ""
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    ai: AIConfig = Field(default_factory=AIConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)


class MCPServerConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    command: str = ""
    enabled: bool = False
    args: list[str] = Field(default_factory=list)


class MCPConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    auto_discover: bool = True
    connection_timeout: int = 30
    servers: list[MCPServerConfig] = Field(default_factory=list)


def _coerce_model(raw: dict[str, Any]) -> ModelConfig:
    """Build a domain ``ModelConfig`` accepting snake_case or legacy camelCase keys."""
    return ModelConfig(
        name=str(raw.get("name", "")),
        base_url=str(raw.get("base_url", raw.get("baseURL", ""))),
        api_key=str(raw.get("api_key", "")),
        streaming=bool(raw.get("streaming", True)),
        max_tokens=int(raw.get("max_tokens", 4096)),
        temperature=float(raw.get("temperature", 0.7)),
        top_p=float(raw.get("top_p", 0.9)),
        timeout=int(raw.get("timeout", 60)),
    )
