# 批次 9-3 方案（R22·A-5 `/todo` 空态英文）

> 只出方案。依据：`docs/r22-feature-matrix.md` §5 A-5、真机帧 `/tmp/r22-shots/13-todo.txt`。

## 1. 调研

`/todo` 有两个空态，来自两条不同路径：

| 场景 | 代码 | 当前文案 |
| --- | --- | --- |
| 本会话还没跑过任何回合（`session.task is None`） | `app/commands.py:241-244` | `[todo] no task list for this session`（**英文**） |
| 跑过回合、任务清单为空 | `domain/task.py::_body_lines:357-359` | `(无任务项)`（已是中文） |

`SessionData.task` 默认 `None`（`domain/session.py:244`），第一轮开始时由 `agent_loop.py:1548-1550`
建 `TaskState(goal=…)`；所以新会话里 `/todo` 走的就是英文分支（帧 13 实测）。

同文件的其它空态已经是中文且带"为什么/下一步"：`[trace] 本会话还没跑过回合（跑完一轮后可看）`
（`commands.py:1323`）、`[budget] 本会话还没跑过回合`（`:1359`）、`[goal] 当前没有在跑的任务。`（`:412`）。

## 2. 方案（一处文案 + 一条测试修正）

**FIX-A5-1：`app/commands.py:244` 改为中文，沿用 `/budget`、`/trace` 的句式：**

```python
console.info("[todo] 本会话还没有任务清单（开始一轮对话后建立）")
```

不改判定逻辑、不加新分支：`task is None` 的含义就是"还没跑过回合"，文案把它说清楚即可。

**FIX-A5-2：`tests/test_todo_command.py::test_todo_no_task_list` 的断言随文案更新。**

它现在断言 `"no task" in ...lower()`，钉的正是要修的英文旧行为（属于"测试写的是旧设计"，允许改）；
改为断言中文关键词存在、英文关键词不存在。

**新增一条**：`test_todo_empty_list_is_chinese`——`TaskState(goal=…)` 无 items 时输出 `(无任务项)`，
锁住第二个空态不漂移。

## 3. 影响面

- 只有控制台/通知行文本；TUI 把它作为 notice 显示，无协议字段；
- 前端、轨迹、导出、数据契约均不变；
- `/stats`、`/skills` 的英文空态**不在本条**：A-6 做空态一致性时统一处理（已记入队列）。

## 4. RIPR

- **Red**：`pytest tests/test_todo_command.py -q`——改断言后现有实现失败（输出仍是英文）；
- **Implement**：改 `commands.py` 一处 + 测试断言/新增用例；
- **Prove**：`pytest tests/test_todo_command.py tests/test_repl.py -q`、`ruff`、`mypy`；
- **Refactor**：无新 helper/分支。

## 5. 真机

`r22-s1` 新会话（不要先跑回合）输入 `/todo`，transcript 应为
`[todo] 本会话还没有任务清单（开始一轮对话后建立）`；帧存 `/tmp/r22-shots/160-*`。

## 6. 完成记录

- 实现：`app/commands.py:244` 改中文；`tests/test_todo_command.py` 断言更新 + 新增空 items 用例；
  `tests/test_repl.py:326` 同步旧英文断言（评审发现的一处遗留，属旧设计断言）。
- 评审修正：初稿"跑过一轮后可看"不准确（裸 goal 不落盘、崩溃恢复会话也可能是 None），
  改为"开始一轮对话后建立"；测试改为断言整句、并加英文负断言。
- 测试：`test_todo_command.py + test_repl.py` 60 例绿；ruff/mypy 干净。
- 真机（帧 160）：`[todo] 本会话还没有任务清单（开始一轮对话后建立）`。
- 已知范围外：`/stats`、`/skills` 的英文空态留给 A-6 统一处理。
