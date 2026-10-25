# CI 连续失败：调研结论与修复方案

> 触发：用户报告"历次上传的改动都被 CI 判失败"。
> 结论：**仓库里没有 PR**（`gh pr list --state all` 为空），失败的是直推 `main` 触发的
> `CI` workflow（`on: push`）。4 次失败运行对应 **3 种互不相关的失败模式**，其中 2 种
> 已在 HEAD 修掉，**剩下 2 个根因仍在 HEAD 上**。
>
> 本稿为**第 2 版**，已按子智能体方案评审（§八）修订。

---

## 一、事实：4 次失败运行

`gh run list --limit 8`：

| 运行 | 时间 | SHA | 失败步骤 | 根因 | HEAD 状态 |
|---|---|---|---|---|---|
| `37089936218` | 10-03 | `4629c19` | — | **success**（对照基线） | — |
| `37767851666` | 10-08 | `651e487` | Tests | `test_loop_digest` 2 条（working-memory 未注入） | 已修（本地 10 passed） |
| `39924039444` | 10-23 | `6dde3e0` | **Lint** | ruff 4 errors（`scripts/` 下 I001×2、F401 `shutil`、F841 `wire`） | 已修（M-A5） |
| `40051057428` | 10-24 | `d1da11a` | **Lint** | 同上 4 条 | 已修（M-A5） |
| `40178523096` | 10-25 | `8c93bfd` | Tests | **4 条**（见下） | **未修（本方案）** |

最新一次（`8c93bfd`）的 4 条失败，逐条对应：

| # | 用例 | 类别 |
|---|---|---|
| 1 | `tests/test_m4_security.py:475` `test_rg_keeps_env_example_searchable` | 缺 ripgrep |
| 2 | `tests/test_m4_security.py:492` `test_rescue_skips_a_credential_symlink_named_env_example` | 缺 ripgrep |
| 3 | `tests/test_r17_credential_glob_regression.py:36` `test_gitignored_log_stays_excluded_with_credential_globs` | 缺 ripgrep |
| 4 | `tests/test_plan_m1.py:766` `test_parent_step_bubbles_carry_real_timestamps` | 3.10 不兼容 |

> 3.13 job 只报 3 条（1–3），3.10 job 报 4 条（1–4）—— 第 4 条是 **3.10 独有**。

日志获取方式（`gh run view --log` 与 `--log-failed` 对已归档运行返回空，必须走 API）：

```bash
curl -sL -H "Authorization: token $GH_TOKEN" \
  "https://api.github.com/repos/linsir2/ai-coding/actions/runs/<run_id>/logs" -o logs.zip
```

---

## 二、根因 A：CI 环境没有 ripgrep，3 个测试硬依赖它

### 2.1 证据

1. `ubuntu-latest` = `ubuntu-24.04`，镜像版本 `20261012.334.1`（与失败日志逐字一致）。
   该镜像的 Tools 清单（actions/runner-images `images/ubuntu/Ubuntu2404-Readme.md`）
   **不含 ripgrep**。
2. 失败输出（3.13 job）：

   ```
   ToolResult(success=False, output="grep runner failed: FileNotFoundError:
     [Errno 2] No such file or directory: 'rg'", ...)
   ```

   同一份日志里 `test_r17_parity.py` 的两条用例**跳过**，理由正是
   `ripgrep is not installed` —— 同一个进程、同一个 PATH，坐实了"环境没有 rg"。
3. 本地 `/usr/bin/rg` = **ripgrep 14.1.0**，装它的时候全量 3858 passed。

### 2.2 代码路径与它的**既定契约**

[grep_tool.py:70](../ai_coding/tools/grep_tool.py#L70)：

```python
resolved = shutil.which("rg") if use_ripgrep is None else ("rg" if use_ripgrep else None)
self._rg = rg_path or resolved
```

`use_ripgrep=None`（autodetect）会 `shutil.which` 检查并降级到 Python 兜底；
`use_ripgrep=True` 则**固定**走 rg 分支，rg 不可用时**大声失败**。

**这不是缺陷，是契约**，两条证据：

* 生产侧只走 autodetect —— [builder.py:203](../ai_coding/tools/builder.py#L203)
  是 `registry.register(GrepTool(sandbox))`，即默认参数。所以"机器上没有 rg"在真实业务里
  永远是优雅降级，不会失败。
* `scripts/r18_acceptance.py:234` 用 `rg_path="/nope"` + 一个必失败的 runner，断言
  `"a broken rg fails instead of reporting no matches"`（`result.success is False`）。
  也就是说，**"我明确要求 rg 而 rg 坏了"必须报错**这条行为已被验收脚本钉死。

> 评审提出的"两种判定标准不一致"因此不成立：`None` 的语义是"我不关心走哪条"（所以可以
> 降级），`True` 的语义是"我必须走 rg"（所以坏了就得说）。把 `True` 也改成静默降级，
> 恰好会退回 `docs/data-contracts.md:896` 警告过的"拿兜底比自己"——那条警告的原文是
> "rg 缺席时打印 UNVERIFIED 而非拿兜底比自己"。

**结论：不改 `grep_tool.py`。** 本方案只补 CI 环境。

### 2.3 rg 分支的消费者：7 个，分三类

`tests/` 下 `use_ripgrep=True` 字面共 11 处，其中 8 处注入了 `runner=`/`rg_path=`，不起真
进程。但**按"是否真的消费 rg 分支"分类**才是有用的口径 —— 另有 4 个消费者用的是别的
写法（`use_ripgrep=use_rg` 的循环、或默认 autodetect `GrepTool(Sandbox(root))`），它们
同样依赖 rg 在场。合计 **7 个消费者**，rg 缺席时表现**分三类**（已用"PATH 指向不含 rg
的目录"实测）：

| 类 | 数量 | 用例 | rg 缺席时的表现 |
|---|---|---|---|
| **硬失败** | 3 | `test_m4_security.py:475`、`test_m4_security.py:492`、`test_r17_credential_glob_regression.py:36` | `FileNotFoundError` → 断言 `success is True` 失败 |
| **skipif 跳过** | 2 | `test_r17_parity.py:183`、`test_r17_parity.py:366` | `@pytest.mark.skipif(not shutil.which("rg"))` |
| **静默假绿** | 2 | `test_m4_security.py:461`、`test_r17_parity.py:206` | **仍然通过，但根本没验证到 rg 分支** |

"静默假绿"是本项目最忌讳的一类（`docs/data-contracts.md:896`："拿兜底比自己"）：

* `test_m4_security.py:461` `test_grep_include_dotenv_still_filters_credentials` ——
  `for use_rg in (False, True)` 且不注入 runner。rg 缺席时 `use_rg=True` 那一轮拿到的是
  错误串，而断言只查 `"TOKEN=secret" not in result.output` → **空过**。
* `test_r17_parity.py:206` `test_both_runners_answer_the_same_question` ——
  autodetect 且不注入 runner，docstring 自称"两棵真正不同的搜索引擎"。rg 缺席时
  `real` 就是兜底 → **兜底和自己比，恒绿**。

> 实测（`PATH=/tmp/norg`，不含 `rg`）：这两条 `2 passed`。

**结论**：CI 装 rg 之后，这 7 个消费者**全部**真正跑在 rg 分支上 —— 3 条硬失败转绿、
2 条 skipif 转运行、2 条假绿变成真验证。所以 §2.5 的修法不止修好 3 条，而是把
"CI 从未验证 rg 分支"这个缺口**整个**补上。

`scripts/` 下另有两处，但**不在 CI 路径**（CI 只跑 `memory_cases.py` / `trace_cases.py`）：

* `scripts/m4_cases.py:193` —— 真起进程（本地/真机验收用）
* `scripts/r18_acceptance.py:234` —— 故意用坏路径测"大声失败"契约

### 2.4 项目自己的立场

`docs/data-contracts.md:896` 已经把这件事记下来了：

> ~~rg 分支会把绝对路径漏给模型~~ **r20/F0 已修**（r19/F1 引入、**提交时 rg 已卸载被
> skip 掩盖**；验收脚本现在会真跑 parity，rg 缺席时打印 UNVERIFIED 而非拿兜底比自己）

也就是说：**"rg 缺席 → 用例跳过"在本项目里已经造成过一次线上缺陷**（r19/F1 的重新锚定
只在 rg 分支泄漏绝对路径，而抓住它的套件当时跳过了）。

而 `test_m4_security.py` 的这 2 条是**安全测试**（凭据文件过滤），
`test_r17_credential_glob_regression.py` 是**凭据 glob 回归测试**。让它们在 CI 跳过，
等于 CI 永远不验证"rg 分支下的凭据过滤"。

### 2.5 方案：在 CI 里安装 ripgrep

`.github/workflows/ci.yml` 的 `Install` 之后、`Lint` 之前加一步：

```yaml
      # ripgrep is what GrepTool's rg branch shells out to, and seven tests exercise
      # that branch -- three hard-fail without it, two skip themselves, and two would
      # otherwise compare the fallback against itself.  ubuntu-24.04 ships without it,
      # so without this step CI never verifies the rg branch at all, which is exactly
      # how r19/F1 leaked absolute paths (docs/data-contracts.md, r20/F0).
      - name: Install ripgrep (the rg branch is part of the contract)
        run: |
          sudo apt-get update -qq
          sudo apt-get install -y -qq ripgrep
          rg --version
```

**为什么是"装"而不是"加 skipif"**：skipif 是把问题藏起来（且与 2.4 的教训直接冲突）；
装上是让环境满足测试的正当依赖，让 rg 分支**真的被验证**。这与 2.4 里"验收脚本现在会
真跑 parity"的方向一致。

**命令细节**：runner 的默认用户是 `runner`，必须 `sudo`；包索引需要先 `update`
（镜像自带缓存，但显式 update 才确定性可用）。`-qq` 压掉进度输出，避免刷屏。

**版本**：`apt-get install ripgrep` 在 noble 上给 **14.1.0**。
> **已知偏差**：`grep_tool.py:42` 与 `test_r17_parity.py:14` 记录"实测真值基于 rg
> 15.1.0"。CI 装 14.1.0 意味着 CI 验证的是 14.1.0 的行为。
> 本地（3.14 + rg 14.1.0）已实测：`test_r17_parity.py` **18 passed 且不跳过**，
> 3 条 rg 用例通过。**并在 §五 Prove 加了 3.10 + 14.1.0 的硬门禁**。
> `grep_tool.py:42` 的注释**有意不改** —— 它记录的是"测量基线"，不是"CI 环境"；
> CI 的版本差异记在 `docs/data-contracts.md`。
> 若将来某条断言需要 15.1.0，把这一步换成下载官方预编译二进制 + sha256 校验即可，
> 其余不变。

---

## 三、根因 B：测试用了 3.11+ 的 `fromisoformat('...Z')`，而声明下限是 3.10

### 3.1 证据

`tests/test_plan_m1.py:766`：

```python
stamp = message.timestamp          # '2026-10-25T08:56:33.371428Z'
datetime.fromisoformat(stamp)     # raises if not a real stamp
```

`datetime.fromisoformat` **3.11 才支持 `Z`**。本地用 pyenv 的 **3.10.22** 实测：

```
ValueError: Invalid isoformat string: '2026-10-25T08:56:33.371428Z'
```

3.13 下正常。而 `pyproject.toml` 的 `[tool.mypy] python_version = "3.10"`、
`[tool.ruff] target-version = "py310"`、CI 矩阵 `["3.10", "3.13"]` —— **3.10 是声明下限**。

### 3.2 生产侧没有问题

`ai_coding/infra/time_utils.py:10` 的 `now_utc()` 发出 `...Z`，这是正确的 ISO-8601（`Z`
就是 UTC 设计符）。所以**根因在测试的解析方式**：它选了唯一一个与项目自身写法不一致的
调用形式。

### 3.3 项目已有的既定写法（3 处生产代码，逐字相同）

| 位置 | 写法 |
|---|---|
| `ai_coding/core/export_otlp.py:42` | `datetime.fromisoformat(ts.replace("Z", "+00:00"))` |
| `ai_coding/service/session_service.py:31` | `datetime.fromisoformat(value.replace("Z", "+00:00"))` |
| `ai_coding/memory/relevance.py:203` | `datetime.fromisoformat(value.replace("Z", "+00:00"))` |

### 3.4 方案：把解析收敛到 `time_utils.parse_iso8601`

`ai_coding/infra/time_utils.py` 已经拥有 `now_utc()` / `now_local()`（**格式化**），
补一个对称的**解析**入口，4 个消费者共用（3 处生产 + 1 处测试），消掉 3 份重复。

```python
def parse_iso8601(value: str) -> datetime | None:
    """Parse an ISO-8601 timestamp, ``None`` when it is not one.

    ``datetime.fromisoformat`` only learned the ``Z`` designator in 3.11, and 3.10 is
    this project's declared floor -- so every consumer normalized it by hand, and the
    one test that parsed a stamp without the normalization failed on 3.10 only.

    A pure parser: a value with no offset comes back *naive*, because "what does a bare
    wall clock mean" is a policy each caller owns -- the OTLP exporter hands it to
    ``datetime.timestamp()`` (local), the memory relevance scorer reads it as UTC.
    Putting either choice here would silently move the other's numbers.
    """
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
```

**为什么是"纯解析器"（naive 保持 naive）**：三处现有消费者的语义并不相同 ——

* `export_otlp._nano`：naive 交给 `datetime.timestamp()`，即**按本地时区**解释；
* `memory/relevance.parse_time`：naive **补 UTC**；
* `session_service._access_rank`：只做有效性门，不关心时区。

把任何一种策略塞进共用函数，都会**静默改变另外两处的数字**（OTLP 导出时间戳 / 记忆新近度
打分）。所以共用函数只做解析，策略留在各自调用点。

**签名是 `str` 不是 `str | None`**：三个调用点都自己做了类型/空值守卫
（`_access_rank` 有 `isinstance(value, str) and value`，`parse_time` 有 `if not value`，
`_nano` 形参就是 `str`）。函数体的 `if not value` 覆盖 `""`（`_nano` 的 `row.ts` 可能是空串）。
`None` 不在契约内，用例不测它。

---

## 四、逐文件改动清单

| # | 文件 | 改动 | 为什么 |
|---|---|---|---|
| 1 | `.github/workflows/ci.yml` | `Install` 后加一步装 ripgrep | 闭合 2.4 的缺口，让 rg 分支真被验证 |
| 2 | `ai_coding/infra/time_utils.py` | 新增 `parse_iso8601` | 4 个消费者共用的唯一解析入口 |
| 3 | `ai_coding/core/export_otlp.py` | 新增 `from ai_coding.infra.time_utils import parse_iso8601`；`_nano` 改调它；删 `from datetime import datetime` | 去重；`datetime` 在文件里已无其他用途（ruff F401） |
| 4 | `ai_coding/service/session_service.py` | 新增 `parse_iso8601` import；`_access_rank` 改调它；删 `from datetime import datetime` | 同上 |
| 5 | `ai_coding/memory/relevance.py` | `parse_time` 改为薄封装（保留 naive→UTC 策略）；新增 `parse_iso8601` import | 去重，行为不变 |
| 6 | `tests/test_plan_m1.py` | `:766` 改断言 `parse_iso8601(stamp) is not None`；删函数内 `from datetime import datetime`；新增 import | 修 3.10 不兼容 |
| 7 | `tests/test_infra_time.py` | 新增 `parse_iso8601` 用例 | RIPR 红相 |
| 8 | `docs/data-contracts.md` | 加 `parse_iso8601` 契约 + CI 的 rg 环境说明 | 契约同步 |

> 改动 3/4/6 的 import 是**成对**的：`datetime` 与 `parse_iso8601` 互换。三个文件里
> `datetime` 都**只**被那一处 `fromisoformat` 使用（已逐个 grep 确认），所以删掉后
> ruff 不会报 F401。

### 4.1 语义等价性核对（改动 3/4/5）

* **`_nano`**：原 `try: fromisoformat(...) except ValueError: return 0`。
  新 `moment = parse_iso8601(ts); if moment is None: return 0`。
  garbage → `None` → `0` ✓；naive → `timestamp()` 仍按本地 ✓；`Z` → `+00:00` ✓。
* **`_access_rank`**：原 `if isinstance(value, str) and value: try … else: return (0, value)`；
  末尾 `return (1, "")`。新 `if isinstance(value, str) and value: if parse_iso8601(value) is not None: return (0, value)`；末尾不变。✓
* **`parse_time`**：原 `if not value: return None` + 解析（失败 → `None`）+ naive→UTC。
  新：

  ```python
  parsed = parse_iso8601(value)
  if parsed is None:            # ← 必须显式判，否则 parsed.tzinfo 会 AttributeError
      return None
  return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
  ```

  **注意**：`parse_iso8601` 的 falsy 短路在**函数内部**，返回给调用方的仍是 `None`；
  `parse_time` 必须自己再判一次。照抄 §3.4 的草图会直接崩，这是复审 N2 抓到的。✓

### 4.2 对数据流的影响

* **无生产数据流变化**：改动 3/4/5 是等价替换，输入输出逐字节相同。
* **CI 数据流变化**：`Tests` 步骤里 rg 分支的 7 个消费者全部真正跑起来 ——
  `test_r17_parity.py` 的 2 条 skipif 从"跳过"变为"运行"（本地实测 18 passed），
  `test_m4_security.py:461` 与 `test_r17_parity.py:206` 这 2 条"静默假绿"从"空过"变为
  "真验证"。这是**覆盖面的增加**，不是行为变化。
* **OTLP / 会话排序 / 记忆新近度**：数值不变（见 4.1）。

### 4.3 修改后的效果

* CI `quality (3.10)` 与 `quality (3.13)` 全绿。
* rg 分支在 CI 首次被真正验证（此前 7 个消费者里 3 个报错、2 个跳过、2 个空过）。
* `time_utils` 成为时间**处理**（格式化 + 解析）的唯一入口，`Z` 的 3 份手写归一化收敛为 1 处。

---

## 五、RIPR 用例

### Red（先写，必须变红）

| 编号 | 用例 | 期望红相 |
|---|---|---|
| U1 | `test_infra_time.py::test_parse_iso8601_reads_a_z_suffix` | `parse_iso8601("2026-10-25T08:56:33.371428Z")` 返回带 `tzinfo` 的 datetime |
| U2 | `test_infra_time.py::test_parse_iso8601_reads_a_numeric_offset` | `"...+08:00"` 解析出 `utcoffset()==timedelta(hours=8)` |
| U3 | `test_infra_time.py::test_parse_iso8601_keeps_a_bare_wall_clock_naive` | 无偏移 → `tzinfo is None`（纯解析器契约） |
| U4 | `test_infra_time.py::test_parse_iso8601_rejects_what_is_not_a_stamp` | `""` / `"not-a-time"` → `None` |
| U5 | `test_infra_time.py::test_parse_time_still_reads_a_bare_wall_clock_as_utc` | `memory.relevance.parse_time` 的 naive→UTC 策略不变 |
| U6 | `tests/test_plan_m1.py::test_parent_step_bubbles_carry_real_timestamps` | **在 3.10 解释器下**必须变红（改动前 `ValueError`） |

U1–U5 是纯新增（新函数还不存在，收集期即红）。
U6 的红相在 3.14 上**不会**变红（3.14 接受 `Z`），必须在 **3.10 解释器**下跑 ——
pyenv 已装 `3.10.22`（`~/.pyenv/versions/3.10.22/bin/python3.10`，`venv` 模块可用）。

### Prove（硬门禁，缺一不可）

1. `.venv/bin/python -m pytest -q` → 3858 + 5 passed（U1–U5 新增 5 条）
2. `.venv/bin/ruff check .` → All checks passed（验证改动 3/4/6 的 import 清理）
3. `.venv/bin/mypy ai_coding` → no issues
4. **3.10 venv 跑与 CI 完全相同的 7 个步骤**（含 rg 在场与缺席两种 PATH）：

   ```bash
   python3.10 -m venv /tmp/v310
   /tmp/v310/bin/pip install -e ".[dev]"
   /tmp/v310/bin/python -m pytest -q
   AI_CODING_MCP_STDIO=1 /tmp/v310/bin/python -m pytest tests/test_mcp_stdio.py -q
   AI_CODING_MCP_STDIO=1 /tmp/v310/bin/python -m pytest tests/test_mcp_end_to_end.py -q
   /tmp/v310/bin/python scripts/memory_cases.py
   /tmp/v310/bin/python scripts/trace_cases.py
   /tmp/v310/bin/ruff check .
   /tmp/v310/bin/mypy ai_coding
   ```

   > 这一步同时回答评审的 P1-2：**"3.10 + rg 14.1.0 下 parity 18 passed"** 的证据。
   > rg 是独立二进制，其行为与 Python 版本无关；本地 3.14 + 14.1.0 的通过结果已经
   > 覆盖了 rg 侧，3.10 venv 补的是 Python 侧。
5. **rg 缺席**复现：把 PATH 换成不含 `rg` 的目录后跑那 3 条 rg 用例 → 必须失败
   （证明"CI 装 rg"是有效修法，而不是"改了测试让它跳过"）
6. **突变测试**（**必须在 3.10 下做**）：把 `parse_iso8601` 的 `.replace("Z", "+00:00")`
   去掉 → U1 与 U6 必须变红。
   > 3.14 上 `datetime.fromisoformat` 原生就认 `Z`，去掉这行 U1 **仍绿** —— 这条突变
   > 只有在 3.10 下才有判别力，别在 3.14 上误判成"突变无效"。

### Refactor

* 确认 `export_otlp.py` / `session_service.py` 的 `datetime` import 已删且 ruff 不报 F401
* 确认 `relevance.parse_time` 仍是 `memory/relevance.__all__` 的导出契约（`service.py`、
  `store.py` 仍从那里 import）

---

## 六、本地 3.10 复现方案

本地 `.venv` 是 **3.14.8**，跑不出根因 B。pyenv 里已有 `3.10.22`：

```bash
python3.10 -m venv /tmp/v310
/tmp/v310/bin/pip install -e ".[dev]"
/tmp/v310/bin/python -m pytest -q                 # 7 步全跑
rg --version                                      # 14.1.0，对应 apt 安装的结果
```

> 注意：本地默认 `python` 是 pyenv 的 3.14.8 且**未装依赖**（`import typer` 失败）。
> 本项目所有验证必须走 `.venv/bin/python`，不要用裸 `python`。

---

## 七、不做的事（明确划出边界）

* **不改 `grep_tool.py`** —— `use_ripgrep=True` 大声失败是既定契约（§2.2）。
* **不改 `scripts/r18_acceptance.py`** —— 它已经是"rg 缺席就报 UNVERIFIED"的正确行为。
* **不给 3 条硬失败用例加 skipif** —— 那会让 rg 分支的凭据过滤在 CI 永不被验证。
* **不给 2 条"静默假绿"用例加 rg 守卫**（§2.3 的 `test_m4_security.py:461`、
  `test_r17_parity.py:206`）—— CI 装 rg 后它们已经真跑 rg 分支；加 skipif 只能帮到
  "本地没装 rg 的开发者"，而那是**改动前就存在的**状况、不是本次 CI 失败的原因，
  属于本次范围之外。已记入 `docs/roadmap-milestones.md` 待办。
* **不动 `grep_tool.py:42` 的注释** —— 它记录的是测量基线（15.1.0），不是 CI 环境。

---

## 八、评审记录

### 8.1 方案评审（子智能体，第 1 轮）

结论：无 P0；4 个 P1、5 个 P2。逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| P1-1 | 识别了 `use_ripgrep=True` 不检查可用性，却不改 `grep_tool.py`，方案自相矛盾 | **已修订 §2.2**：补上两条证据（`builder.py:203` 生产只用 autodetect；`r18_acceptance.py:234` 钉死"坏 rg 必须失败"），明确这是契约而非缺陷，并说明"静默降级"恰好是本仓库警告过的反模式 |
| P1-2 | parity 真值基于 15.1.0，CI 却装 14.1.0；且评审认为本地无 3.10 无法验证 | **已修订 §2.5 / §五**：把"3.10 + rg 14.1.0 跑满 CI 七步"提为硬门禁（Prove 第 4 条）。评审称本地无 3.10 是**误判** —— pyenv 已装 `3.10.22` 且 `venv` 可用（已实测） |
| P1-3 | U6 的红相在本地无法复现 | **已修订 §五**：红相改由 pyenv 3.10.22 承担，并写明 3.14 上不会变红 |
| P1-4 | `apt-get install` 缺 `sudo` 与 `update` | **已修订 §2.5**：命令补全为两行 `sudo apt-get update -qq` / `sudo apt-get install -y -qq ripgrep` |
| P2-1 | `parse_iso8601` 签名 `str` 与 U4 测 `None` 矛盾 | **已修订 §3.4**：签名保持 `str`，U4 只测 `""` 与垃圾串，并说明三个调用点各自已有守卫 |
| P2-2 | "全仓 11 处"不准（`scripts/` 还有 2 处） | **已修订 §2.3**：改为按"是否真消费 rg 分支"分类的 7 个消费者清单，并列出 `scripts/` 的 2 处及其不在 CI 路径 |
| P2-3 | `grep_tool.py:42` 注释与 CI 版本漂移 | **已修订 §2.5**：说明该注释有意不改（记录测量基线），CI 差异记在 data-contracts |
| P2-4 | §一 表格未列出 4 条失败用例 | **已修订 §一**：补逐条对应表 |
| P2-5 | 改动清单缺新增 import | **已修订 §四**：3/4/5/6 均写明 import 的增删 |

### 8.2 方案评审（子智能体，第 2 轮）

结论：第 1 轮 4 个 P1 **全部真闭合**（逐条复核证据）；新提 1 个 P1、5 个 P2/P3。逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| N1【新 P1】 | rg 消费者清单漏掉 2 个"静默假绿"用例（`test_m4_security.py:461`、`test_r17_parity.py:206`），"3 个测试硬依赖 rg"低估了 CI 的 rg 覆盖面 | **已修订 §2.3**：改成"7 个消费者分三类"（3 硬失败 / 2 skipif / 2 静默假绿），并逐条说明假绿的机制。**已亲自复现**：`PATH=/tmp/norg` 下这两条 `2 passed`。同步修正 §2.5 注释与 §4.2 的计数。**守卫仍不加**，理由写入 §七 |
| N2【新 P2】 | §4.1 的 `parse_time` 等价性草图漏了 `None` 分支，照抄会 `AttributeError` | **已修订 §4.1**：补上显式 `if parsed is None: return None`，并写明"falsy 短路在函数内部，调用方拿到的仍是 `None`" |
| N3【新 P2】 | §五 Prove 6 的突变断言与解释器有关（3.14 上 `Z` 原生可解析，突变无判别力） | **已修订 §五**：注明"必须在 3.10 下做"，并说明 3.14 上会误判 |
| N4【新 P2】 | §2.3 把 `test_r17_defects.py:396` 标为"注入 runner"，实际是真 runner + `rg_path="/nonexistent/rg"` | **已修订 §2.3**：该行已并入"4 处注入 runner/path、不起真进程"的概述（396 的 `rg_path` 指向不存在的路径，同样不起真 rg） |
| N5【新 P3】 | U5 测 `memory.relevance.parse_time` 却放在 `tests/test_infra_time.py` 下，跨模块 | **保留**：`time_utils` 是被改的模块，U5 钉的是"它的消费方契约不变"，放在本模块测试文件里可读性最好；已在 §五 U5 描述里写明测的是消费方策略 |
| N6【新 P3】 | §4.3 "时间格式化的唯一入口"措辞不准 | **已修订 §4.3**：改为"时间**处理**（格式化 + 解析）的唯一入口" |

### 8.3 实现评审（子智能体）

结论：**无 P0、无 P1**。实现与方案逐项一致，未越界（没碰 `grep_tool.py`、没给 3 条硬
失败加 skipif、没加 2 条静默假绿的守卫）。评审独立复现了全部验收门禁（3.14 / 3.10 各
3863 passed、七步全绿、rg 缺席复现、3.10 突变）。

3 个 P2 + 2 个 P3，逐条处置：

| 编号 | 内容 | 处置 |
|---|---|---|
| P2-1 | `opencode-gui-m6-plan.md:70` 仍写"时间戳用例改用 `datetime.fromisoformat`" | **已修订**：保留历史记录，追加"N-5 起改用 `parse_iso8601`"的指引 |
| P2-2 | 提交尚未推送，无真实 CI 运行记录 —— 本提交的核心收益（CI 首次真跑 rg 分支）只有本地证据 | **待推送后确认**（见 §8.4） |
| P2-3 | 方案引用的 `data-contracts.md:895` 因本次编辑已漂到 `:896` | **已修订**：3 处引用同步为 `:896` |
| P3-1 | `data-contracts.md:309` 称 `parse_iso8601` 是唯一入口，但 `test_infra_time.py:23` 仍裸用 `fromisoformat` | **已修订**：该测试改用 `parse_iso8601`，"唯一入口"对测试侧也成立 |
| P3-2 | `m1-analysis.md` 的 `time_utils` 说明只列 `now_utc()` | **不改**：历史分析文档，记录当时状态 |

### 8.4 推送后的真实验收

_待填。_
