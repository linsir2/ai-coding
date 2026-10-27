"""前端定位与首次安装（H3 红相）。

``launcher`` 今天只有一条路径：``parents[2]/"frontend"``——那是**源码树**的
位置，装成 wheel 之后它指向 site-packages 的上一层，TUI 直接起不来。

这里钉四件事：

* ``AI_CODING_FRONTEND`` 覆盖优先，且**不拷贝**（开发热改）；
* 装过的 checkout（有 ``node_modules``）就地用，不复制 124MB；
* 否则复制到 ``data_home()/frontend/<version>``，且只复制一次；
* ``bun`` 的入口必须在**运行目录**下——从包内绝对路径启动，``bun`` 会从
  入口文件所在目录向上找 ``node_modules``，找不到就 ``Cannot find package``。
"""

from __future__ import annotations

from pathlib import Path

import pytest

import ai_coding.server.launcher as launcher


def _frontend(tmp_path, *, with_node_modules: bool = False) -> Path:
    root = tmp_path / "frontend"
    (root / "src").mkdir(parents=True)
    (root / "src" / "index.tsx").write_text("// entry\n", encoding="utf-8")
    (root / "package.json").write_text("{}", encoding="utf-8")
    if with_node_modules:
        (root / "node_modules").mkdir()
    return root


class _Result:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode


class _Runner:
    """Records every ``bun install`` and creates ``node_modules`` like the real one."""

    def __init__(self, *, returncode: int = 0, creates: bool = True) -> None:
        self.calls: list[list[str]] = []
        self.returncode = returncode
        self.creates = creates

    def __call__(self, argv, *, cwd, timeout):
        self.calls.append(list(argv))
        if self.creates:
            (Path(cwd) / "node_modules").mkdir(exist_ok=True)
        return _Result(self.returncode)


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("AI_CODING_FRONTEND", raising=False)


def test_the_override_wins_and_is_not_copied(tmp_path, monkeypatch):
    checkout = _frontend(tmp_path)
    monkeypatch.setenv("AI_CODING_FRONTEND", str(checkout))

    assert launcher._frontend_dir() == checkout
    assert launcher._runtime_dir(checkout, in_place=True) == checkout
    assert not (tmp_path / "data" / "ai-coding" / "frontend").exists()


def test_the_packaged_frontend_is_used_without_an_override(tmp_path):
    assert launcher._frontend_dir() is None
    assert launcher._PACKAGED_FRONTEND.name == "frontend"
    assert launcher._PACKAGED_FRONTEND.parent.name == "ai_coding"


def test_a_checked_out_frontend_is_used_in_place(tmp_path):
    checkout = _frontend(tmp_path, with_node_modules=True)

    assert launcher._runtime_dir(checkout) == checkout


def test_the_packaged_frontend_is_copied_once(tmp_path, monkeypatch):
    packaged = _frontend(tmp_path)
    copies: list[tuple[str, str]] = []
    real = launcher.shutil.copytree

    def counting(src, dst, **kwargs):
        copies.append((str(src), str(dst)))
        return real(src, dst, **kwargs)

    monkeypatch.setattr(launcher.shutil, "copytree", counting)

    first = launcher._runtime_dir(packaged)
    second = launcher._runtime_dir(packaged)

    assert first == second
    assert len(copies) == 1
    assert (first / "src" / "index.tsx").is_file()
    assert not (first / "node_modules").exists(), "node_modules must not be copied"


def test_the_entry_point_lives_in_the_runtime_dir(tmp_path):
    runtime = _frontend(tmp_path)

    entry = launcher._require_entry(runtime)

    assert entry == runtime / "src" / "index.tsx"
    assert entry.parent.parent == runtime


def test_missing_node_modules_installs_once(tmp_path):
    runtime = _frontend(tmp_path)
    runner = _Runner()

    launcher._ensure_deps(runtime, "/usr/bin/bun", run=runner)
    launcher._ensure_deps(runtime, "/usr/bin/bun", run=runner)

    assert runner.calls == [["/usr/bin/bun", "install", "--frozen-lockfile"]]


def test_an_installed_directory_never_runs_bun(tmp_path):
    runtime = _frontend(tmp_path, with_node_modules=True)
    runner = _Runner()

    launcher._ensure_deps(runtime, "/usr/bin/bun", run=runner)

    assert runner.calls == []


def test_a_failed_install_is_a_value_error_naming_the_command(tmp_path):
    runtime = _frontend(tmp_path)
    runner = _Runner(returncode=1, creates=True)

    with pytest.raises(ValueError) as caught:
        launcher._ensure_deps(runtime, "/usr/bin/bun", run=runner)

    message = str(caught.value)
    assert "bun install" in message
    assert str(runtime) in message
    assert not (runtime / "node_modules").exists(), "a half-install must be removed"


def test_a_missing_frontend_names_the_override(tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()

    with pytest.raises(FileNotFoundError) as caught:
        launcher._require_entry(empty)

    assert "AI_CODING_FRONTEND" in str(caught.value)
