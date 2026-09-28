"""验证旧 CLI 子命令已删除，REPL 是默认入口。"""

import subprocess
import sys


def test_ask_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "ask", "--prompt", "hello"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_config_check_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "config-check"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_memory_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "memory", "list"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0


def test_trace_command_does_not_exist():
    result = subprocess.run(
        [sys.executable, "-m", "ai_coding", "trace", "list"],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode != 0
