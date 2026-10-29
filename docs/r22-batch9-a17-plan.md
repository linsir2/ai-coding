# 批次 9-2 方案（R22·A-17 失败卡片标题仍说"已运行命令"）

> 只出方案，不改代码。依据：`docs/r22-feature-matrix.md` §8 末段 A-17、
> A-4 真机帧 156（`✗ 已运行命令 · ls` + 副标题"沙箱不可用（命令未执行）"）。

## 1. 调研

- 卡片标题由 `util/toolmeta.ts:81-84` 的 `toolTitle(verb, target, running)` 生成：
  `running ? "正在" : "已"` + verb，**完全不看结果**。
- `component/toolcard.tsx:82` 只传 `running()`；同一文件已经算好了 `denied()`、
  `failure().tone` 与 `props.part.status`（`"running" | "ok" | "failed"`）。
- 于是所有失败/被拒卡片都写成 `✗/⊘ 已运行命令 · ls`：
  - `sandbox_unavailable` 的副标题是"沙箱不可用（命令未执行）"，与"已运行"直接矛盾；
  - `timeout`、`spawn_failed`、`tool_error`、审批拒绝、门卫拦截同样受影响。
- 成功卡片（✓）与运行中（spinner）的文案是对的；副标题已经用一句话说明了失败原因，
  标题不需要再重复原因，只需要**不说假话**。

## 2. 方案（一处签名 + 一处传参）

**FIX-A17-1：`toolTitle` 增加第四个参数 `ok = true`。**

```ts
/** `正在运行命令 · ls` / `已运行命令 · ls`；失败时是 `已尝试运行命令 · ls`。 */
export function toolTitle(
  verb: string, target: string, running: boolean, ok = true,
): string {
  const action = running ? `正在${verb}` : ok ? `已${verb}` : `已尝试${verb}`
  return target ? `${action} · ${target}` : action
}
```

**FIX-A17-2：`toolcard.tsx` 把真实结果传进去。**

```tsx
{toolTitle(props.part.verb, props.part.target, running(), props.part.status === "ok")}
```

默认值让既有三参调用（测试、其它卡片）行为不变；只有卡片把失败显式传进来。

### 为什么是"已尝试"，不是按 tone 分"失败/未"

- `failed → {verb}失败`、`denied → 未{verb}` 更精确，但要按工具动词做中文搭配：
  `询问用户失败`、`收尾研究失败`、`未提交计划` 都能读，`未询问用户` 也勉强——却多了一条
  按 tone 分支的文案契约，而副标题已经把原因说清楚了。
- "已尝试"对两种 tone 都成立（调用确实发生过），一句话解决，不需要维护词表。
- 若未来需要更精确，可在标题后追加状态词；本轮不做。

## 3. 影响面

- 只改**前端卡片标题文本**：失败/被拒卡片从 `已{verb}` 变成 `已尝试{verb}`；
- 成功、运行中、副标题、图标、删除线、时长、展开行为全部不变；
- 无数据流、无事件、无后端、无导出契约变化；
- 既有 `toolTitle(..., 3 args)` 调用保持原行为（默认 ok=true），测试不受影响。

## 4. RIPR（Red → Implement → Prove → Refactor）

### Red（今天必须失败）

`test/toolmeta.test.ts`：
| 用例 | 断言 |
| --- | --- |
| `a failed call does not claim it ran` | `toolTitle("运行命令","ls",false,false) === "已尝试运行命令 · ls"`（今天 `已运行…`） |
| `a denied call also does not claim it ran` | `toolTitle("编辑文件","a.py",false,false) === "已尝试编辑文件 · a.py"` |
| 既有 `正在/已` 两例 | 保持，三参调用默认成功 |

`test/toolcard-render.test.tsx`：
| 用例 | 断言 |
| --- | --- |
| `a failed card says it tried, not that it ran` | 失败 part（`error:"sandbox_unavailable"`）渲染帧含 `已尝试运行命令 · ls`、`沙箱不可用（命令未执行）`，不含 `已运行命令` |
| 既有 `a denial is struck through` | 增加断言帧含 `已尝试编辑文件`（删除线仍保留） |

### Implement
`util/toolmeta.ts` → `component/toolcard.tsx` → 测试。

### Prove
`cd ai_coding/frontend && bun test test/toolmeta.test.ts test/toolcard-render.test.tsx`；
全量 `bun test` + `bun run typecheck`。

### Refactor
无新 helper、无分支表；`toolTitle` 仍是纯函数。

## 5. 真机与验收

1. 用 A-4 的影子 `bwrap`（`PATH=/tmp/fakebin:$PATH` + `/tmp/config.enforce.yaml`）在 `r22-s1`
   提交 `run ls in the workspace`，批准后卡片应为
   `✗ 已尝试运行命令 · ls · …（审批 …）` + `沙箱不可用（命令未执行）`；帧存 `/tmp/r22-shots/159-*`；
2. 成功调用（例如 `glob`）仍为 `✓ 已查找文件…`；
3. 前端全量测试 + `tsc`；后端不受影响；
4. 文档：矩阵 A-17 划掉、队列进度更新。

## 6. 完成记录

- 实现：`toolTitle` 第 4 参 `ok = true`；`toolcard.tsx` 传 `status === "ok"`；卡片头注释同步。
- 测试：`toolmeta.test.ts` 44 例（失败/被拒两条新例）；`toolcard-render.test.tsx` 新帧测（失败卡片含
  `已尝试运行命令 · ls`、不含 `已运行命令`）；前端全量 **556 通过 / 0 失败**，`tsc` 干净。
- 代码评审：无阻塞；采纳"头注释过期"修正；运行态帧测不可行（渲染 harness 无 `spinner` 组件，
  运行分支由 `toolmeta.test.ts` 覆盖），已注明。
- 真机（帧 159，`/tmp/r22-shots/159-a17-attempted.txt`）：

  ```
  ✗ 已尝试运行命令 · ls · 53.6s（审批 53.6s）
  沙箱不可用（命令未执行）
  ```

