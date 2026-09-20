"""TDD (M5-1): MCP integration — manager, config wiring, service/factory assembly.

All offline: constructing ``MCPServerStdio`` objects never connects to a server;
connection happens at run time in the SDK.  We verify config parsing, filtering,
and that servers flow into the AI service / agent.
"""

import pytest

from ai_coding.ai.agents_sdk_service import AgentsSDKChatService
from ai_coding.ai.factory import build_service_from_config
from ai_coding.config.models import AppConfig, MCPConfig, MCPServerConfig
from ai_coding.core.mcp import MCPManager
from ai_coding.domain.model_config import ModelConfig


def _model() -> ModelConfig:
    return ModelConfig(name="m", base_url="u", api_key="k")


@pytest.fixture
def enabled_cfg() -> MCPConfig:
    return MCPConfig(
        enabled=True,
        servers=[
            MCPServerConfig(name="github", command="npx",
                            args=["-y", "@modelcontextprotocol/server-github"], enabled=True),
            MCPServerConfig(name="off", command="npx", args=["x"], enabled=False),
            MCPServerConfig(name="", command="npx", args=["y"], enabled=True),
        ],
    )


def test_mcp_manager_disabled_returns_empty():
    mgr = MCPManager(MCPConfig(enabled=False, servers=[
        MCPServerConfig(name="a", command="c", enabled=True),
    ]))
    assert mgr.build_servers() == []


def test_mcp_manager_filters_disabled_and_blank_name(enabled_cfg):
    servers = MCPManager(enabled_cfg).build_servers()

    # only the "github" entry survives (one enabled, non-blank server)
    assert len(servers) == 1
    assert "github" in {s.name for s in servers}
    assert "off" not in {s.name for s in servers}


def test_mcp_manager_skips_blank_command():
    servers = MCPManager(MCPConfig(enabled=True, servers=[
        MCPServerConfig(name="bad", command="", enabled=True),
    ])).build_servers()
    assert servers == []


def test_mcp_manager_empty_config_gives_empty():
    assert MCPManager(MCPConfig()).build_servers() == []


def test_service_holds_and_attaches_mcp_servers(enabled_cfg):
    servers = MCPManager(enabled_cfg).build_servers()
    svc = AgentsSDKChatService(model_config=_model(), mcp_servers=servers)

    assert list(svc._mcp_servers) == servers


def test_service_build_agent_carries_mcp_servers(enabled_cfg):
    servers = MCPManager(enabled_cfg).build_servers()
    svc = AgentsSDKChatService(model_config=_model(), mcp_servers=servers)

    agent = svc._build_agent()
    assert getattr(agent, "mcp_servers", []) == servers


def test_factory_defaults_mcp_absent_to_empty():
    cfg = AppConfig(default_model="m", models={"m": _model()})
    svc = build_service_from_config(cfg)

    assert svc._mcp_servers == []


def test_factory_builds_from_config_mcp(enabled_cfg):
    cfg = AppConfig(
        default_model="m", models={"m": _model()},
        mcp=MCPConfig(enabled=True, servers=[
            MCPServerConfig(name="github", command="npx", args=["-y","s"], enabled=True),
        ]),
    )
    svc = build_service_from_config(cfg)

    assert len(svc._mcp_servers) == 1


def test_factory_disabled_mcp_yields_no_servers():
    cfg = AppConfig(
        default_model="m", models={"m": _model()},
        mcp=MCPConfig(enabled=False, servers=[
            MCPServerConfig(name="a", command="c", enabled=True),
        ]),
    )
    svc = build_service_from_config(cfg)

    assert svc._mcp_servers == []


def test_app_config_exposes_mcp_default():
    cfg = AppConfig(default_model="", models={})
    assert cfg.mcp.enabled is False
    assert cfg.mcp.servers == []
