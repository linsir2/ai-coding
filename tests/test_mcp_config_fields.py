"""MCP-11: the gated progress fixture must use fields that exist.

``MCPServerConfig``/``MCPConfig`` are ``extra="ignore"``, which is deliberate
(an old user config keeps loading) but also silent: the progress fixture carried
``connection_timeout`` and ``enabled``, both dropped on the floor, so it was
really running against the 60s startup default.  This file is ungated so the
field check runs in every environment.
"""

from test_mcp_progress import _config

from ai_coding.config.models import MCPTimeout


def test_the_progress_fixture_uses_current_config_fields():
    cfg = _config()
    server = cfg.servers[0]

    assert isinstance(server.timeout, MCPTimeout)
    assert server.timeout.startup == 30_000
    assert server.disabled is False
    assert "connection_timeout" not in cfg.model_dump()
    assert "enabled" not in server.model_dump()
