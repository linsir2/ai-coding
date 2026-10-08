"""Volatile state filter for memory harvest."""

import pytest

from ai_coding.config.models import MemoryConfig
from ai_coding.memory.service import MemoryService, _is_volatile_note
from ai_coding.memory.store import MemoryStore


@pytest.mark.parametrize("text", [
    "当前已完成项目骨架、依赖安装、database.py 和 models.py，下一步是实现 schemas.py",
    "接下来要写 tests",
    "刚才运行了 pytest 全部通过",
    "/tmp/hotel_smoke.db 里的测试数据已清空",
    "下一步要实现 rooms.py",
    "当前进度：已完成 3/5 步",
    "当前正在处理用户登录模块",
    "刚刚完成了用户注册接口",
])
def test_volatile_patterns_are_rejected(text):
    assert _is_volatile_note(text) is True


@pytest.mark.parametrize("text", [
    "酒店项目使用 FastAPI 和 SQLite",
    "write/edit 工具只能写工作区内文件",
    "用户偏好：读取文件时只输出请求的内容",
    "项目使用 FastAPI 0.142.4",
])
def test_durable_facts_pass(text):
    assert _is_volatile_note(text) is False


@pytest.mark.asyncio
async def test_harvest_skips_volatile_candidate(tmp_path):
    async def extract(messages, conclusion):
        return [
            {"text": "当前已完成骨架，下一步是实现 schemas.py", "kind": "fact"},
            {"text": "酒店使用 FastAPI", "kind": "fact"},
        ]

    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, extractor=extract, config=MemoryConfig())
    await svc.harvest([
        __import__("ai_coding.domain.message", fromlist=["ChatMessage"])
        .ChatMessage(role="user", content="hi", timestamp="t")
    ])
    assert len(store.all()) == 1
    assert store.all()[0].text == "酒店使用 FastAPI"


def test_write_note_rejects_volatile(tmp_path):
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=MemoryConfig())
    report = svc.write_note("接下来要实现 bookings.py")
    assert report.added == ()
    assert store.all() == []


def test_shape_and_volatile_are_independent(tmp_path):
    """形状门拒碎片，易变门拒进度，两者正交。"""
    store = MemoryStore(tmp_path / "m.json")
    svc = MemoryService(store, config=MemoryConfig())
    # JSON 碎片 → 形状门
    svc.write_note('"text": "AI-Coding 的编排')
    # 进度态 → 易变门
    svc.write_note("当前已完成骨架")
    # 正常 → 入库
    svc.write_note("酒店使用 SQLite")
    assert len(store.all()) == 1
