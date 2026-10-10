"""TDD (M1): display helpers — the provider label shown in the prompt metadata."""

from __future__ import annotations

from ai_coding.server.display import provider_from_base_url


class TestProviderFromBaseUrl:
    def test_skips_api_prefix(self):
        assert provider_from_base_url("https://api.deepseek.com/v1") == "deepseek"

    def test_strips_path_and_port(self):
        assert provider_from_base_url("http://localhost:8000/v1") == "localhost"

    def test_ordinary_host(self):
        assert provider_from_base_url("https://openrouter.ai/api/v1") == "openrouter"

    def test_www_prefix_skipped(self):
        assert provider_from_base_url("https://www.example.com") == "example"

    def test_no_scheme_still_parses(self):
        assert provider_from_base_url("api.siliconflow.cn") == "siliconflow"

    def test_empty_input(self):
        assert provider_from_base_url("") == ""

    def test_garbage_input(self):
        assert provider_from_base_url("not a url at all") == ""

    def test_bare_host_with_port(self):
        assert provider_from_base_url("localhost:11434") == "localhost"
