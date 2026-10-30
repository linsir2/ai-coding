"""D: a bare path in the prompt names a file, it does not inline it.

Measured live 2026-10-30: every iteration carried 22-28k characters of
``<referenced-files>`` because the wave seed mentions ``backend/app/models.py``
and friends, and ``resolve_bare_paths`` treats any workspace path a message names
as a request to paste the file in.  Compaction cannot touch that block (it is a
volatile tail block), so it sat in every request next to a history the pipeline
kept shrinking -- a quarter of the request was a file list the model could have
read on demand.

The rule these tests pin (Cursor's "dynamic context discovery"): an explicit
``@file`` still inlines; a bare path becomes a pointer with the read tool named.
Directories keep their listing -- a listing is how the model picks a file to read.
"""

from ai_coding.ai.engine import ChatEngine
from ai_coding.config.models import AIConfig
from ai_coding.core.agent_loop import AgentLoop
from ai_coding.domain.loop import StepResult
from ai_coding.service.session_service import SessionService
from ai_coding.tools.registry import ToolRegistry

PAYLOAD = "BARE-PATH-PAYLOAD-42"


class _Engine(ChatEngine):
    async def step(self, messages, *, tools, instructions=None, on_delta=None):  # type: ignore[no-untyped-def]
        return StepResult(text="ok", finish_reason="stop")

    async def aclose(self) -> None:
        return None


def _block(tmp_path, text: str) -> str:
    root = tmp_path / "ws"
    (root / "backend" / "app").mkdir(parents=True)
    (root / "backend" / "app" / "models.py").write_text(
        "\n".join(f"# line {i} {PAYLOAD}" for i in range(400)), encoding="utf-8"
    )
    loop = AgentLoop(
        _Engine(),
        SessionService(tmp_path / "sessions"),
        ToolRegistry(),
        workspace=str(root),
        settings=AIConfig(stop_policy="natural", max_tool_iterations=3),
    )
    return loop.assembler._referenced_files_block(text)


def test_a_bare_path_is_a_pointer_not_an_inlined_body(tmp_path):
    block = _block(tmp_path, "看看 backend/app/models.py 这个文件")

    assert "backend/app/models.py" in block
    assert PAYLOAD not in block, "a bare path must not paste the file into every request"
    assert "read" in block, "a pointer is only useful if it names the tool that fetches it"


def test_an_explicit_at_reference_is_still_inlined(tmp_path):
    block = _block(tmp_path, "@backend/app/models.py")

    assert PAYLOAD in block, "the operator asked for this file by name"


def test_a_directory_keeps_its_listing(tmp_path):
    block = _block(tmp_path, "@backend/app")

    assert "models.py" in block
    assert "line 0" not in block, "a listing lists names; it does not read bodies"
