# eval-08 · H3 之后的全链路真机复测

> 触发：`/workspace/ai-coding-评估报告.md`（2026-10-24 外部实测，HEAD `d1da11a`）与
> [roadmap-milestones.md](roadmap-milestones.md)（8 领域 / 32 里程碑）。
> 目的：**不是再读一遍代码，而是把改造后的东西真机跑一遍** —— 从输入到输出、含长任务，
> 看功能是否真起作用、差距在哪里、有没有为了"修好"而新弄坏的地方。
> 环境：Linux 6.x / Python 3.14 / tmux 3.4 / bubblewrap 0.9.0 / 模型 `dashscope qwen3.7-flash`
> 起点：`f5ce59d`（H3 `/connect` + 模型切换）→ I1 修复 `747ad99` → 本文件收口的 3 个新缺陷 `7cbc9f5`

---

## 0. 一句话结论

**28/32 里程碑仍成立，改造确实在起作用**（A/B/C/D/E 五阶段全绿：冒烟、8 场景、长时机制、
TUI 闭环、质量门禁 6/6；离线全量 **4320 passed / 0 failed**）。但真机也暴露了
**3 个"改了但没改对"的地方**，本轮已修并各自带用例：

1. **截断回合把旁白当答案** —— 10-24 报告里"63 次调用 / 900 秒 / 零结论"的一半就是它：
   模型在工具步骤上写了"让我读下一个文件"，收尾话被这句旁白顶掉，用户只看到 30 字。
2. **计数型收敛档（16 次只读）选错了目标** —— 它截断的是"18 次调用、18 个新发现"的合法
   设计调研，而它要治的 61 次空转回合夹着 `bash`、streak 最高 7。默认改回关闭，
   只留证据档。
3. **工具叫 bash，跑的却是 dash** —— 模型写的 `mkdir -p src/{components,pages}` 在 dash 下
   **退出 0 却建出一个名叫 `{components,pages}` 的字面目录**，长任务因此白跑一轮。

阶段 F（90 分钟全栈长任务）**没有完成**：后端 pytest 仍红、前端未 build。原因见 §2 阶段 F，
其中一条就是我们自己的 shell 缺陷。

未达成的仍是三项产品化：**M-H1 部分**（无 PyPI/CHANGELOG）、**M-H3 IDE 集成**（未开始）、
**M-H4 插件市场签名**（未开始）。T2 的"≤16 次调用"目标**没有达到**（42 次），
但回合现在**以结论收尾**而不是以旁白收尾 —— 见 §4.2。

---

## 1. 环境与入口（实测）

| 项 | 实测值 |
|---|---|
| 解释器 | `/workspace/ai-coding/.venv/bin/ai-coding`（**不在 PATH**） |
| 配置 | 必须带 `--path config.local.yaml`；不带会整条丢弃项目层 `qwen`，用户层缺 key 时直接 `MissingSecret` |
| 沙箱 | `mode=enforce`，bwrap 0.9.0 可用；默认 `network.mode=deny`（沙箱内 bash 无出网） |
| 前端 | `ai_coding/frontend/node_modules` 在包内 → 就地运行，不联网 |
| 环境差异 | **工具会话自带的沙箱不允许非特权 user namespace**，所以容器内跑 `pytest` 时 5 条 bwrap 用例 skip；**关掉该沙箱后 53 passed / 2 skipped**（§3.0） |

---

## 2. 六阶段真机跑

### 阶段 A · 冒烟（`scripts/integration_smoke.py`）

```
model     : qwen
base_url  : https://dashscope.aliyuncs.com/compatible-mode/v1
======================================================== [1] tool turn
roles=['user', 'assistant', 'tool', 'assistant'] tool_msgs=1
tool_calls=[ToolCallRef(id='call_7cccb008...', name='read', arguments='{"path": ".../sample.txt"}')]
======================================================== [2] follow-up replay
replied(69 chars): 'sample.txt 文件的内容是一句简单的英文文本："hello world from workspace"...'
======================================================== [3] sub-agent delegation
subAgent tool messages=1 ledgers=3
packet preview: '{"status": "completed", "iterations": 2, "todos": [], "artifacts": [], "ledger_path": ".../subagents/fb618b80eea6.json"...'
======================================================== [4] L4 summarizer + memory extractor
summary=OK len=240
notes   =[HarvestCandidate(text='用户使用 Python 进行后端开发，偏好简洁的代码风格', kind=PREFERENCE, importance=4, scene='开发偏好', scope='workspace')]
======================================================== SMOKE OK
```

### 阶段 B · 8 场景（`scripts/sample_tasks.py`）

```
  PASS  enabled=False servers=0
[read]        PASS  read_calls=2 answer='`sample.txt` 的内容如下：alpha / beta'
[todo]        PASS  todo_calls=4 planned=2 survived=2 {'1': 'completed', '2': 'completed'}
[write+edit]  PASS  write=True edit=True file='one|TWO|three'
[bash-ok]     PASS  bash_calls=1 succeeded=1
[bash-deny]   PASS  gate_denied=0 executed=0 model_refused_without_calling=True
[subagent]    PASS  subagent_calls=1 ledgers=1
[watchdog]    PASS  elapsed=20.0s status=timeout bash_calls=2 budgeted=True pairing_ok=True
SUMMARY 8/8 passed
```

### 阶段 C · 长时机制（`scripts/eval_longtask_mechanics.py`）

```
turn 1..10: reply=41/6/6/6/6/6/6/6/6/6 chars, msgs 8→30
after turns: persisted=0 elided=1 msgs=30
L3 probe: persisted=1 marker='<persisted-output path=".../persisted/...'
  [PASS] L3 persists an oversized trailing tool result
compact_session -> {'messages': 30, 'after': 23, 'removed': 7}
  [PASS] history stays bounded (<= max_messages)         23 messages
  [PASS] L2 elides an aged tool result to a content-addressed file
         path=.../elided/read_d468160f464f.txt elided=2
[3] turn watchdog   elapsed=8.1s reply=189 chars
  [PASS] watchdog bounded the turn (<=20s)               8.1s
============================================================
  6 passed, 0 failed
```

### 阶段 D · TUI 真机闭环（tmux，真实任务）

`capture-pane` 原文（节选）：

```
   ▄████▄ ██  ▄█████ ▄████▄ ████▄  ██ ███  ██  ▄████
   ┃  Ask anything… "Fix broken tests"
   ┃  Build · dashscope · qwen3.7-flash
```

任务：`读取 buggy.py，找出会导致运行时异常的真实缺陷，用 edit 工具修好，然后运行 python buggy.py`

```
     缺陷很明显：当传入空列表 [] 时，len(values) 为 0，导致 ZeroDivisionError。
     ⠼ 正在编辑文件 · /workspace/eval-workspace/tui-i1/buggy.py
     ...
  ┃  △ Permission required
  ┃  运行命令 · python buggy.py
  ┃  tool 'bash' requires confirmation
     ...
     ✓ 已运行命令 · python buggy.py · 1m 19s
     2.0 0
```

落盘证据：`turn-1.jsonl` 4 个迭代 / 3 次工具 / `TurnFinished status=COMPLETED
reason="model finished" iters=4`；session JSON 8 条消息，`read → edit → bash` 全在；
`.ai-coding/sessions/write-ahead/` 已清空（回合结束释放）。独立复跑 `python3 buggy.py`
得到同样输出 `2.0` / `0`。

### 阶段 E · 同上下文对比（`scripts/eval_comparison.py`）

| 任务 | 10-24 基线 | 10-28 改造后（计数档 16） | 10-28 收口后（计数档关 + 收尾修复） |
|---|---|---|---|
| T1 读代码 | 27.6s / 1 调用 / 2,181 字 | 39.9s / 2 调用 / 3,204 字 | — |
| T2 找缺陷 | **900s / 63 调用 / 32 字** | 683s / 61 / **44 字** | 554s / 42 / **846 字（诚实结论）** |
| T3 设计 | 183.6s / 30 / 6,734 字 | 83.5s / 18 / **30 字** | **113.4s / 11 / 7,096 字** |

**这三行是本轮最重要的证据**，逐条解释在 §4.2。

T3 收口后的原文开头（`COMPLETED`，6 个迭代）：

```
基于对 agent_loop.py、turn_policy.py、interventions.py 和 config/models.py 的阅读…
结论：加在 AgentLoop._model_step() 这一层，作为 engine 之外的一层包装。
| 层级 | 为什么不合适 | Engine 内部 → 纯 I/O…  TurnPolicy/interventions → 管行为模式，不管 HTTP…
```

### 阶段 F · 长时全栈（`scripts/eval_fullstack_task.py --rounds 14`）

**结果：没有完成。** 90 分钟超时被 `timeout` 杀掉，连 `eval_task_log.json` 都没写出来
（脚本只在 14 轮跑完或判定通过时才落盘）。可核实的产物与状态：

```
backend/  5 个文件 343 行（database.py 27 / main.py 203 / models.py 64 / schemas.py 49 / __init__.py）
          tests/test_api.py 278 行
          $ python -m pytest -q backend  → 18 failed（全部 "no such table: tags"）
frontend/ 只有空目录 + 一个名叫 {components,pages,types,services} 的字面目录（见 §4.3）
          没有 package.json → build 门形同未开始
session  433 条消息 / 5 个回合 / 146 份归档 transcript
          工具计数：read 112、bash 79、write 15、edit 9、todo_write 6
```

**卡点不是"不会写"，是同一个错反复修**。session 里模型自己的独白连着五遍几乎同一句：

```
从错误信息看，核心问题是 no such table: tags …
错误根因已定位：测试 fixture 中 Base.registry.metadata.create_all() 创建的数据库里没有 tags 表 …
错误根因：no such table: tags — SQLAlchemy 的 create_all() 没有创建 tags 表 …
错误依然是 no such table: tags — 说明表根本没有被创建 …
```

而 harness 的检测**全部触发了**，只是没有人类可答：

```
user [search-gate]   （2 次）
user [strategy-gate] 仍无进展：相同参数的失败调用从现在起会被直接拦截…
tool <tool_output tool="read" ok="false"> {"decision": "deny",
     "reason": "'read' already succeeded 2 time(s) this turn with the same arguments…"}
user [strategy-gate] 连续失败且换手段无效。现在必须用 ask_user 提问…
turnDigest: outcome = "迭代预算用尽：no progress: no tool call has succeeded in 2 iterations"
```

**结论（如实记）**：单次 14 轮无人值守驱动**不再复现** 10-24 的 TaskFlow 结果
（那一次是 `--keep` 两轮驱动）。三个可分离的原因：

1. **我们自己的 shell 缺陷**：`{components,pages}` 字面目录白烧一轮（§4.3 已修）；
2. **模型的 SQLAlchemy 死循环**：同一根因五连改、每次换个等价的改法，`DuplicateCallGuard`
   拦住重复读之后它就换参数继续读同一个文件；
3. **round 驱动放大了循环**：每一轮把同一段 pytest 输出重新喂回去，而 `ask_user` 在
   无头驱动里没有回答者 —— 检测器说"去问人"，没有人，下一轮又从头开始。

第 2、3 条是**新立项的候选**（不属本轮修复范围）：收敛判据只看 `read/grep/glob` 的连续
streak 与重复证据，而"写→跑→读→写"的交替会同时重置两者，所以这种环路对现有判据**不可见**。

---

## 3. 离线门禁（与 CI 同一组命令）

### 3.0 沙箱：真内核

```
$ .venv/bin/python -m pytest tests/test_process_sandbox.py   # 关掉工具沙箱
53 passed, 2 skipped   （跳过的是"bwrap 可用时反而不跑"的两条反向守卫）
```

### 3.1 CI `quality` job 的 6 个脚本

```
red line benchmark OK                      （135 条语料，FN=0/FP=0 硬阈值）
permission gate benchmark OK               （58 条，1 条 known_miss：grep 绝对越界路径）
memory_cases.py    SUMMARY 11/11 passed
trace_cases.py     SUMMARY 7/7 passed
retrieval_cases.py --check  guardrails OK  （reachable R@3>=0.95 / FP@5<=0.5）
r18_acceptance.py  36 passed, 0 failed
```

### 3.2 静态与全量

```
ruff check .   → 0
mypy ai_coding → 0（164 files）
pytest         → 4320 passed, 9 skipped（0 failed, 210.77s）
```

9 条跳过全部是**设计如此**：6 条 MCP stdio（需 `AI_CODING_MCP_STDIO=1`）、2 条
"bubblewrap 可用时反而不跑"的反向守卫、1 条 live 层门。**在工具会话自带的沙箱里**
（无 user namespace）同一套用例是 `4297 passed / 29 skipped`（多出 5 条 bwrap 用例按声明跳过）。

### 3.3 本轮 15 项缺陷涉及的定向回归

```
pytest tests/test_mcp_oauth.py tests/integration/test_r17_defects.py tests/test_openai_engine.py
       tests/test_summary_shrink.py tests/test_interventions.py tests/test_undo_command.py
       tests/test_memory_store.py
186 passed
```

---

## 4. 真机跑出来的三个问题（已修，各自带用例）

### 4.1 截断回合把旁白当答案（`34226dc`）

**现象**：同一道 T3 题跑两次，一次返回 30 字（"Let我先快速确认现有 retry 相关代码的具体位置。"），
另一次返回 900+ 字的收尾报告。

**根因**：`render_handoff` 的判断是"`outcome.text` 非空 → 模型已经说过话 → 不需要报告"。
但真实模型会在**带工具调用的那一步**写旁白，这句旁白落进 `outcome.text`，把
**收尾调用**（`tools=[]`，专门为截断回合要的结论）挤掉了。`_finish_turn` 又先返回
`outcome.text`，于是旁白成了操作者看到的"答案"。

**修法**：① 有收尾话就出报告；② 返回时报告优先于 text；③ 顺带把 `_CLOSING_PROMPT` 从
"说明完成了什么、还差什么"改成"**直接回答用户的问题**" —— 实测旧措辞（eval-08 的 T2）
得到 900 字的"我读了这十个模块"，仍然没有缺陷。

**验收**：新增 `test_narration_between_looks_does_not_swallow_the_closing`（红相原文
`assert '收尾：结论如上' in '让我先读下一个文件。'`）；两条变异（改回旧的
`render_handoff` 条件、改回 `text or handoff`）各自把该用例打红。

### 4.2 计数型收敛档默认关闭（`84f7167`）

**现象**：T3 从基线的 30 次调用 / 6,734 字，变成 18 次 / 30 字。

**trace 证据**（`trace-T3/.ai-coding/traces/*/turn-1.jsonl`）：

```
SearchStalled {"iteration": 3,  "level": 1, "calls": 5,  "streak": 5,  "stale_streak": 0, "novel": 5}
SearchStalled {"iteration": 5,  "level": 2, "calls": 8,  "streak": 8,  "stale_streak": 0, "novel": 8}
SearchStalled {"iteration": 8,  "level": 2, "calls": 13, "streak": 13, "stale_streak": 0, "novel": 13}
SearchStalled {"iteration": 11, "level": 3, "calls": 18, "streak": 18, "stale_streak": 0, "novel": 18,
               "unconverged": true, "reason": "search unconverged"}
TurnFinished  {"status": "MAX_ITERATIONS", "reason": "search unconverged", ...}
```

`stale_streak=0`、`novel=18`：**18 次只读调用带来的全是新发现**，没有一次重复证据。
计数档却把它当成"空转"拦腰截断 —— 它选的是"连续读文件"，不是"没有新信息"。

**另一个方向的证据**（T2 的 trace，`trace-T2`）：49 次工具调用 = `bash 25 / read 14 /
grep 6 / glob 4`，只读 streak 最高 7（`bash` 每次把它清零），计数档**根本不会触发**。

**修法**：`AIConfig.search_streak_l3` 默认 `16 → 0`（关），`TurnPolicyConfig` 同步；
默认只剩证据档（`stale_l3=6` 且 `repeated_nonempty>=2`）。计数档保留为 opt-in。

**验收**：`test_the_product_default_for_the_look_count_is_off`（默认值改回 16 即红）；
关档后重跑 T3：**113.4s / 11 调用 / 7,096 字 / COMPLETED**，比基线还短、答案还长 ——
因为 L1/L2 的两级提示本来就在起作用，硬截断是多余的那一刀。

### 4.3 工具叫 bash，跑的却是 sh（`7cbc9f5`）

**现象**（阶段 F 产物）：`frontend/src/` 下出现一个**名叫 `{components,pages,types,services}`
的字面目录**，而 `components/ pages/ types/ services/` 四个真目录也建了 —— 模型写的是
`mkdir -p src/{components,pages,types,services}`，dash 不实现大括号展开，**退出码 0**，
前端脚手架因此白跑一轮。

**修法**：`BashTool` 与 `JobRegistry` 共用的唯一入口 `spawn_argv` 的 argv[0] 从写死的
`/bin/sh` 改为 `SHELL_ARGV0`（`/bin/bash` 优先，Alpine 回落 `/bin/sh`）。

**连带发现的真缺陷**：`ulimit -f` 的块大小**随 shell 变**（POSIX/dash 512 字节，bash 1024），
`_ULIMIT_BLOCK = 512` 让 64KB 的作业日志上限在 bash 下变成 **128KB**
（`test_a_job_whose_log_passes_the_cap_is_killed_and_says_so` 实测恰好 2 倍）。
改为按 `SHELL_ARGV0` 取块大小。

**验收**：新增 `test_the_shell_understands_brace_expansion`（真子进程 `mkdir -p src/{a,b}`，
断言建出两个目录、且不存在字面目录）；jobs 的日志上限用例在 bash 下重新变绿。

---

## 5. 15 项缺陷：当时 vs 今天

| # | 10-24 | 今天 | 证据 |
|---|---|---|---|
| P0-1 `import httpx2` | 远程 MCP/OAuth 100% 崩 | ✅ 已修（`core/mcp.py`；依赖收紧 `mcp>=1.24,<2`） | `test_mcp_oauth.py` 18 passed |
| P0-2 undo 部分回滚未上报 | 只读目录下报 `ok:True` | ✅ 已修（`restore_ok` + `_rewrote_history`） | `test_r17_defects.py`、`test_undo_command.py` 全绿 |
| P0-3 `open_all()` 契约 | 文档 `[]` / 代码 `None` | ✅ 文档对齐（物理上不可能返回列表：M3 起非阻塞） | `sample_tasks` mcp-off `enabled=False servers=0` |
| P1-4 无重试分类 | 额度耗尽不切备用 | ✅ `QUOTA` 档 + **首次即切**（本轮 R-1） | `test_openai_engine.py`；`WORTH_ANOTHER_MODEL={AUTH_ERROR,QUOTA,...}` |
| P1-5 无收敛判据 | 63 次调用无结论 | ⚠️ **部分**：证据档（stale≥6）在；**计数档默认关**（本轮实测它选错目标） | §4.2；`test_interventions.py` |
| P1-6 静态检查回喂 | 无 | ✅ 已有（`core/static_check.py` + L2 注入），但**默认关** | 全部件通过；真机 T2 里模型自己跑了 ruff/mypy |
| P1-7 恢复粒度 | 会话级 | ✅ step 级 write-ahead sidecar（回合结束清空） | `test_*` 全绿；TUI 的 `write-ahead/` 回合后为空 |
| P1-8 沙箱只是路径限制 | 无 OS 级隔离 | ✅ bwrap + 策略/机制分离，fail-closed | **真内核 53 passed** |
| P1-9 压缩不如竞品精细 | 无预计算 | ✅ 收缩阶梯补 0.85；**预计算经调研不做**（摘要请求的 prompt 前缀与主请求不重合，cache 命不中；opencode 自己也默认关） | `_SHRINK_STEPS=(0.85,0.7,0.5,0.35)` |
| P1-10 L3 语义未文档化 | — | ✅ 写进 `features.md`（回合中段护栏，会话 JSON 看不到） | 阶段 C 实测 `persisted=1` |
| P2-11 ruff/mypy | 4 / 6 | ✅ 0 / 0（164 文件） | §3.2 |
| P2-12 memory 用例与文档不一致 | case7/case11 | ✅ 11/11，文档改成路径无关的不变量 | §3.1 |
| P2-13 r7 L2 预算 | 未达预算 | ✅ 判据载体改对（48000 窗口 → 阈值 24000，实测 29%） | r7 用例 |
| P2-14 记忆并发写 | 非线程安全 | ✅ 锁内 `_save` + `flock` + 唯一 tmp | `test_memory_store.py` |
| P2-15 Esc/`/review` 退化 | — | ✅ 双击 esc；`/review` 走普通回合 | 交互用例 |

**新发现（不在 15 项里）**：§4.1 / §4.2 / §4.3 三项 —— 全部是"改造引入或改造没盖住"的问题，
不是原报告条目。

---

## 6. 32 里程碑逐条（判据 → 今天）

| 领域 | 状态 | 本轮复核证据 |
|---|---|---|
| **A** A1–A6（6） | ✅ 6/6 | 定向 186 passed；ruff/mypy 0；`memory_cases` 11/11；`sample_tasks` mcp-off |
| **B** B1✅ B2✅ B3✅ B4⚠️不采纳 B5✅ | ✅ 4/5 | B3 的 `insufficient_quota` 今天是 `QUOTA` 且首次即切（R-1）；B2 的 L1=4/L2=6 与代码一致 |
| **C** C1–C3（3） | ✅ 3/3 | 全量用例；TUI 回合后 write-ahead 清空 |
| **D** D1✅ D2✅ D3✅ | ✅ 3/3 | 真内核 53 passed；redline FN=0/FP=0；grants TTL + `/permissions revoke`（**面板需人看**） |
| **E** E1–E4（4） | ✅ 4/4 | `CompactionEnded.layers` 实测有 token 差（阶段 C 的 L2/L3 账）；**`/trace` 渲染需人看** |
| **F** F1✅ F2✅ F3✅ | ✅ 3/3 | `memory_cases` 11/11；`memory_latency_cases` 可复跑；**文档口径需人看** |
| **G** G1–G4（4） | ✅ 4/4 | CI `quality` 6 个脚本全绿；`trace overview` 有 CLI；三层 marker；权限基准 |
| **H** H1⚠️ H2✅ H3⬜ H4⬜ | ⚠️ 1.5/4 | H1：wheel 内容有 CI 断言，但**无 PyPI/CHANGELOG**；H2：`${VAR}` + `config init`；H3/H4 未开始 |

**合计 28 达成 / 1 不采纳 / 3 未达成**（与 I1 方案 §1.2 一致）。
24 条机检项全部跑到；6 条"需人看"的（D3/E1/E3/F2/G2/H2 的面板或文档部分）本轮只做了
可机检的一半，**面板类没有截图证据**，这里如实标注。

---

## 7. 仍未达成的差距（不美化）

| 差距 | 事实 | 为什么这轮没修 |
|---|---|---|
| **T2 的 ≤16 次调用** | 收口后 42 次 / 554s（基线 63 / 900s） | 计数档被证明选错目标（§4.2）；真正能压住的是"证据不增长"，而 T2 的 25 次 `bash` 让证据一直在变。要么给"检索总预算"另立契约（TB 级），要么接受 40 轮迭代上限 |
| **T2 没找到缺陷** | 收尾结论是"未能确定一个明确的运行时异常" + 已排除项清单 | 10-24 报告找到的 `import httpx2` **已被 M-A1 修掉**；今天的仓库可能确实没有同等显眼的运行时缺陷。结论诚实，不是失败 |
| **阶段 F 未完成** | 90 分钟 / 5 个回合 / 0 个可交付前端；后端 18 failed | 三条原因见 §2 阶段 F。其中"写→跑→读→写"的环路**对现有收敛判据不可见**（两条判据都被 ACTION 重置）—— 新立项候选，不在本轮 |
| **无人值守里 `ask_user` 没有回答者** | 策略门两次要求提问，没有人 | 这是 round 驱动的固有形态，不是产品缺陷；但驱动脚本应该把"已问过且无人答"当成升级信号（换会话 / 重规划），而不是把同一段日志再喂一次 |
| **M-H1** | 无 PyPI、无 CHANGELOG | 发布是运维动作，不是代码缺口 |
| **M-H3** | 全仓无 LSP/editor 代码 | 32 条里唯一不可机检项；需要独立立项 |
| **M-H4** | `skills/` 无签名校验 | 同上 |
| **bwrap 在"工具会话沙箱"内不可用** | 容器内 5 条 skip，关掉工具沙箱 53 passed | 这是**评估环境**的限制，不是产品缺陷；产品在真实主机上 fail-closed + 明确原因码 |

---

## 8. 复现

```bash
cd /workspace/ai-coding
P=.venv/bin/python

# 阶段 A/B/C（真网络）
$P scripts/integration_smoke.py       --config config.local.yaml
$P scripts/sample_tasks.py            --config config.local.yaml
$P scripts/eval_longtask_mechanics.py --config config.local.yaml

# 阶段 E（同上下文对比；注意它会覆写 /workspace/eval-workspace/comparison_ai_coding.json）
$P scripts/eval_comparison.py --config config.local.yaml --repo /workspace/ai-coding

# 阶段 F（新目录，避免 rmtree 掉 10-24 的产物）
$P scripts/eval_fullstack_task.py --config config.local.yaml \
   --workspace /workspace/eval-workspace/taskflow-i1 --rounds 14

# 门禁
$P scripts/redline_benchmark.py; $P scripts/permission_benchmark.py
$P scripts/memory_cases.py; $P scripts/trace_cases.py
$P scripts/retrieval_cases.py --check; $P scripts/r18_acceptance.py
$P -m pytest -q
```

TUI（tmux）：

```bash
tmux new -d -s i1
tmux send-keys -t i1 "cd /workspace/ai-coding && .venv/bin/ai-coding \
  --path config.local.yaml --workspace /workspace/eval-workspace/tui-i1" Enter
tmux capture-pane -p -t i1
```
