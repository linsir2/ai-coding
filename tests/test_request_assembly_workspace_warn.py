"""TDD (hotel-fix P0-1 Build补洞): environment block warns outside-workspace paths."""

from ai_coding.core.request_assembly import RequestAssembler
from ai_coding.core.turn_policy import TurnPolicy, TurnPolicyConfig
from ai_coding.domain.message import ChatMessage
from ai_coding.domain.session import SessionData


class _Compactor:
    async def compress(self, history, **kwargs):
        return history


class _Memory:
    def block(self):
        return "", []


class _Reporter:
    def report_context(self, **kwargs):
        return "canonical"

    def tokens_spent(self):
        return 0


class _Settings:
    working_memory_chars = 1200
    auto_compact = False


def _assembler(workspace: str) -> RequestAssembler:
    return RequestAssembler(
        compactor=_Compactor(),
        policy=TurnPolicy(TurnPolicyConfig()),
        memory=_Memory(),
        reporter=_Reporter(),
        settings=_Settings(),
        emit=lambda e: None,
        workspace=workspace,
    )


def _session_with(text: str) -> SessionData:
    session = SessionData(
        session_id="s1", title="", created_time="t", last_access_time="t"
    )
    session.messages.append(
        ChatMessage(role="user", content=text, timestamp="t")
    )
    return session


def test_outside_path_warns_with_workspace():
    asm = _assembler("/home/linsir365/projects/ai-coding")
    session = _session_with(
        "请设计 /home/linsir365/projects/awesome-tool-kit/hotel 系统"
    )
    block = asm._environment_block(session)
    assert "工作区:" in block
    assert "/home/linsir365/projects/ai-coding" in block
    assert "工作区外" in block
    assert "-w" in block


def test_inside_path_no_warn():
    asm = _assembler("/home/linsir365/projects/ai-coding")
    session = _session_with("看看 /home/linsir365/projects/ai-coding/ai_coding 的结构")
    block = asm._environment_block(session)
    assert "工作区外" not in block


def test_no_absolute_path_no_warn():
    asm = _assembler("/home/linsir365/projects/ai-coding")
    session = _session_with("请设计一个酒店管理系统，最简模式即可")
    block = asm._environment_block(session)
    assert "工作区外" not in block
