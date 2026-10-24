# eval-05 · 长时全栈工程任务（TaskFlow）

> 命题：评估方自拟。驱动：`scripts/eval_fullstack_task.py`（真机，多轮 + 自检回喂）。
> 工作区：`/workspace/eval-workspace/taskflow`

## 1. 命题

> **「TaskFlow」任务管理系统** —— 可运行的前后端全栈应用：
> - **后端**：Python + FastAPI + SQLite，REST API（CRUD、状态流转 `todo→in_progress→done`、
>   多标签、关键字搜索、按状态/标签统计），**自带 pytest 测试（≥6 个）**，监听 8000
> - **前端**：React + TypeScript + Vite，页面（列表 / 看板 / 新建编辑 / 统计），调用后端 API，
>   监听 5173
> - **交付**：根 `README.md` 写明启动步骤；后端 pytest 全绿；前端 `npm run build` 成功

## 2. 驱动方式

无头多轮（`handle_input`），每轮后跑两道硬门（后端 pytest、前端 build），
把失败输出**回喂**给下一轮：

```
round 1  → 出方案/搭骨架
round 2+ → 实现；自检；失败则把 pytest / build 输出贴回下一轮
round N  → 两道门都过 → TASK COMPLETE
```

配置：`max_tool_iterations=30`、`turn_timeout_seconds=1800`、`max_auto_continuations=4`。

## 3. 最终产物（实测验证）

```
$ cd backend && python -m pytest -q
11 passed, 100 warnings in 0.32s

$ cd frontend && npm run build
dist/index.html                   0.45 kB │ gzip:  0.29 kB
dist/assets/index-4VLmdZdP.css    0.40 kB │ gzip:  0.28 kB
dist/assets/index-DP-WqMpD.js   283.61 kB │ gzip: 90.78 kB
✓ built in 181ms
```

### 后端（9 个 Python 文件，452 行）

```
backend/app/__init__.py
backend/app/database.py       # SQLAlchemy engine + session
backend/app/main.py           # FastAPI app + CORS
backend/app/models.py        # Task ORM 模型
backend/app/schemas.py      # Pydantic 请求/响应
backend/app/routers/tasks.py  # CRUD / 状态流转 / 搜索 / 统计
backend/tests/conftest.py     # 临时 SQLite + TestClient
backend/tests/test_tasks.py   # 11 个测试（覆盖 API）
backend/requirements.txt
```

### 前端（真实组件，493 行）

```
frontend/src/App.tsx                    # 页面路由（list/kanban/stats/new/edit）
frontend/src/api.ts                     # axios 封装（34 行）
frontend/src/types.ts                   # Task / TaskCreate / TaskUpdate（40 行）
frontend/src/components/TaskList.tsx    # 列表（94 行）
frontend/src/components/KanbanBoard.tsx # 看板（79 行）
frontend/src/components/TaskForm.tsx    # 新建/编辑（90 行）
frontend/src/components/TaskCard.tsx    # 卡片（75 行）
frontend/src/components/StatsPanel.tsx  # 统计（81 行）
```

> **注意**：`README.md` **未生成**（两轮驱动都在 README 之前被中止/耗时过长）。
> 这是本次任务**唯一未满足的交付项**。

## 4. 关键发现

### 4.1 ⭐ 门通过 ≠ 任务完成（最值得注意）

第一轮驱动里，前端**先**被建成 Vite 脚手架模板：

```tsx
// 第一轮的 frontend/src/App.tsx —— 纯 Vite starter
function App() {
  const [count, setCount] = useState(0)
  return (<><h1>Get started</h1><p>Edit <code>src/App.tsx</code> ...</p>
    <button onClick={() => setCount(count+1)}>Count is {count}</button></>)
}
```

- `npm run build` **通过**（脚手架当然能 build）→ 自检门**放行**。
- 但任务要求的「列表/看板/新建编辑/统计」**一个都没实现**。
- 直到第二轮驱动（`--keep` 续跑）才产出真实组件。

**判读**：这是**代理式长任务的典型失败模式** —— 只要验收门是「可机器判定的副产物」
（build 成功 / 测试通过），模型就可能用**最小满足**而非**任务本身**去过门。
本项目有 `session.acceptance` + 验收门机制（`docs/features.md` R0），
但**本次任务没有填写验收项**，所以门只校验了「build/test」而非「功能齐全」。

### 4.2 自检门的假阴性（评估方自己的 bug，已修）

第一版驱动的 `self_check` 用了 `pytest --timeout=60`，而 `pytest-timeout` 不在
ai-coding 的 venv 里 → pytest 以 `unrecognized arguments` **退出码 4** →
驱动**永远**认为后端测试失败 → 反复把同一段错误回喂给模型。

```
$ python -m pytest -q --no-header -x --timeout=60
ERROR: python -m pytest: error: unrecognized arguments: --timeout=60
```

**证据**：同一份代码，用系统 python 直接跑是 `11 passed`。
**修复**：`scripts/eval_fullstack_task.py` 去掉 `--timeout`（commit `42662c0`）。

> 这条对**产品**是正面证据：模型的 `bash` 自检**自己装上了 `pytest-timeout`**
> （工具输出里可见 `pytest-timeout 2.4.0`），只是装进了系统 python 而非 venv ——
> 说明它**确实在尝试让自检跑通**，而不是绕开门。

### 4.3 长时行为：自动续跑被大量触发

第一轮驱动的会话里，合成 user 消息（`[continue]` / `[strategy-gate]`）堆积：

```
user msgs: 15（其中非合成 6 条）
  0 请在当前工作区从零构建…                                    ← TASK
  1 后端测试还没通过… ERROR…                                  ← 自检回喂
  2 [strategy-gate] 最近 2 轮的工具调用全部失败…               ← 合成
  3 [strategy-gate] …                                          ← 合成
  4 [strategy-gate] 仍无进展：相同参数的失败调用从现在起会被直接拦截 ← 合成
  5 后端测试还没通过…                                         ← 自检回喂
  6 [strategy-gate] …
  7 [continue] 上一轮预算耗尽，但还有未完成的任务项：…          ← 合成
  …
```

第二轮（`--keep`）的会话更极端：**265 条 user 消息**（大量 `[continue]`）、527 条消息、171 次工具调用。

**判读**：`max_auto_continuations` 的续跑**确实在起作用**（否则回合早就结束了），
但**成本很高**：续跑提示本身要占上下文，且每轮续跑都重发工具 schema。
这与 `docs/features.md` 里 C3 续跑的设计一致，但**实测显示它是长时任务的主要 token 来源**。

### 4.4 观察到的产品行为（正面）

- 模型**主动**用 `bash` 跑 pytest / npm build 自检（这是任务要求，但它执行得很干净）。
- 后端**先**搭完并让测试转绿，**再**做前端 —— 符合「分步骤实现」的要求。
- 前端组件**按职责拆分**（List / Kanban / Form / Card / Stats / api / types），不是一坨文件。
- 生成的代码**可运行、可测试、可构建**（不是伪代码）。

## 5. 结论

| 维度 | 结果 |
|---|---|
| 后端 | ✅ FastAPI + SQLAlchemy + SQLite，**11/11 测试通过** |
| 前端 | ✅ React + TS + Vite，**真实组件 493 行**，build 通过 |
| README | ❌ 未生成 |
| 端到端启动 | ⚠️ 未做（后端/前端各自可启动，但未联调验证） |
| 耗时 | 第一轮 ≈1h49m（未收敛）；第二轮 ≈45m+（产出真实前端） |
| 工具调用 | 第一轮 333 次；第二轮 171+ 次 |
| 主要失败模式 | **门通过 ≠ 任务完成**；自检门假阴性（评估方 bug）；续跑成本高 |

**最重要的结论**：这个项目**能**写出可运行的全栈项目（后端测试全绿、前端 build 通过、
组件按职责拆分），但**长时任务里它会用「最小满足」去过机器可判定的门** ——
这不是 bug，而是**验收契约设计问题**：本项目已有 `session.acceptance` 机制，
只要填对验收项就能防住；本次没填，所以门形同虚设。
