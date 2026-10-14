# dsh 学习笔记 01 · Cordis 底座

> 来源：`docs/cordis-primer.md`、`docs/cordis-tutorial/{01,02,03,04,06}.md`、`vendor/README.md`。
> 一句话：**Cordis 是 dsh 底下的插件框架。不理解它，后面 58 个包全是雾。**

---

## 1. 为什么 Cordis 是第一个必须吃透的东西

dsh 的每一个能力——工具、LLM 适配器、文件访问、**甚至 agent loop 本身**——都是挂进共享 `ctx` 的插件。Cordis 只提供五件事：**插件、上下文、服务、类型化事件、可逆副作用**。所有"可替换""可组合""无特权内核"的承诺，都建立在这五个原语上。

Cordis 是**源码 vendor 进仓库**的（`vendor/cordis`，上游 `cordiverse/cordis` v4.0.0-rc.7，固定 commit），dsh 完全拥有自己的框架层——可审计、可 patch、可钉版本。`vendor/README.md` 记了 23 条本地修改（多数是生命周期加固与文档补全）。

---

## 2. 五个核心概念（primer 原文的"五句话"）

| # | 概念 | 一句话 |
|---|---|---|
| 1 | **插件 = 实现 Service 的对象** | 可以是带 `apply(ctx)` 的函数，也可以是 `Service` 子类 |
| 2 | **上下文 = 服务的仓库** | 服务从 ctx 认领稳定的 `ctx.<key>`（`ctx.tools`/`ctx.llm`/`ctx.sessions`）；别的插件**按 key 找服务，不 import 具体实现** |
| 3 | **依赖用 `inject` 声明** | 声明所需服务的插件会**等待**它们出现——**加载顺序由服务依赖表达，不是手动 boot 排序** |
| 4 | **类型化事件通信** | 服务用 TS declaration merging 声明事件名，再按语义选 `emit`/`waterfall`/`parallel`/`serial`/`bail` 派发 |
| 5 | **注册即可逆副作用** | prompt 分节、工具 schema、适配器、provider、listener 全走 `ctx.effect()`/`ctx.on()`，reload/teardown 时**可预测地回收** |

---

## 3. 三种插件形态

```ts
// 1. 函数插件（最常用）
export function apply(ctx: Context) {}

// 2. 对象插件
export const objectPlugin = { name: 'object-plugin', apply(ctx: Context) {} }

// 3. 类插件：Service 子类（需要对外暴露服务时用）
export class MyService extends Service {
  constructor(ctx: Context) { super(ctx, 'myTutorialService') }
}
```

- `export const name = '...'` 是**可选显示元数据**，只用于诊断。
- **`apply` 抛异常 = 响亮失败，不是跳过**（tutorial 01 的"try breaking it"）。但**模块无法 resolve**（路径/包名拼错）只走 logger，boot 早期可能丢失——"新加的 entry 没反应，先查拼写"。
- 一个 entry 的 **`disabled: true`** 可保留 entry 但跳过挂载；翻回来（及其 PENDING 的依赖）会重新加载。

---

## 4. 生命周期与 fiber 状态机（关键）

每个已加载插件实例拥有一个 **fiber**：

```
PENDING → LOADING → ACTIVE → UNLOADING → DISPOSED
                 ↘ FAILED
```

| 状态 | 含义 |
|---|---|
| **PENDING** | 已声明，但**某个 required service 还不存在** |
| LOADING / ACTIVE | `apply` 正在跑 / 已跑完 |
| FAILED | `apply` 或 config 校验抛了 |
| UNLOADING / DISPOSED | disposer 正在跑 / 已拆干净 |

> **PENDING 是"插件为什么什么都不打印"的最常见答案。** 它是合法状态（provider 可能稍后才挂），所以**没有错误、没有输出**。诊断方式：遍历 `ctx.registry.values()` → `runtime.fibers` → 找 `fiber.state === FiberState.PENDING`（tutorial 06）。

`fiber.dispose()` 在**所有 cleanup（含 async disposer）跑完**后才 resolve，并递归卸载子插件。

---

## 5. Effects：可逆副作用（"注册即副作用"的机制）

**Cordis 管不到的资源**（timer、连接、watcher）要自己包进 `ctx.effect()` 并返回 disposer：

```ts
ctx.effect(() => {
  const timer = setInterval(...)
  return () => clearInterval(timer)   // 卸载时自动调
})
```

**大部分注册本来就是 effect**，不用你手写：
- `ctx.on(event, listener)` —— 卸载时移除 listener（**永远不用手动 `removeListener`**）
- `ctx.plugin(child)` —— 子插件随父一起 dispose
- 服务注册、`ctx.tools.register(...)` —— 返回的 disposer 自动挂到调用插件上

**顺序陷阱**（tutorial 02）：disposer 按注册的**逆序**启动，但**多个 async disposer 并发跑**。若 teardown 必须串行，**放进同一个 disposer 里 await**。

---

## 6. Services + inject：依赖驱动加载

```ts
// 提供方
declare module '@deepseek-ai/cordis' { interface Context { greeter: GreeterService } }
export class GreeterService extends Service {
  constructor(ctx: Context) { super(ctx, 'greeter') }   // 运行期：注册到 ctx.greeter
  greet(who: string) { return `Hello, ${who}!` }
}

// 消费方
export const inject = ['greeter']
export function apply(ctx: Context) { console.log(ctx.greeter.greet('world')) }
```

两个机制协作：
- **运行期**：`super(ctx, 'greeter')` 注册实例，**注册即 effect**——provider 卸载则服务消失。
- **编译期**：`declare module` 把 `greeter` 加进 `Context` 接口，`ctx.greeter` 才类型安全；**它不生成任何代码**。

**两条硬规则**：
1. **加载顺序由依赖决定，不由 `cordis.yml` 里的行序决定。** entry 并发启动，换行序输出不变；删掉 provider，consumer 永远 PENDING，**不崩溃、不部分运行**。
2. **`inject` 不是一次性 boot 检查。** 运行中 provider 消失（卸载或热替换）→ **所有依赖它的插件被卸载**；服务回来时再加载。这就是"换 provider 就能换掉整个产品行为"的机制。

**可选依赖**：不写 `inject`，在使用点 `ctx.get('greeter')` 探测（无 provider 时为 `undefined`，插件照常跑）。

---

## 7. Events：五种派发模式 + waterfall

事件名用 `namespace/action` 约定保持扁平命名空间可读。

| 模式 | 调用 | 语义 |
|---|---|---|
| `emit` | `ctx.emit(name, ...args)` | 同步广播；**不 await、不收集**返回值 |
| `parallel` | `await ctx.parallel(...)` | 所有 listener **并发**跑，一起 await |
| `serial` | `await ctx.serial(...)` | 按序 await；**第一个非 null/false/undefined 返回值胜出并停止后续** |
| `bail` | `ctx.bail(...)` | serial 的同步版 |
| `waterfall` | `ctx.waterfall(name, ...args, next)` | **around-middleware**（见下） |

**派发模式是事件公开契约的一部分**：新 harness 事件用 `@mode` 标签标注，生成的目录会**核对声明与派发点**。

### Waterfall = 拦截的引擎

listener 收到 `(...args, next)`：
- 调 `next()` → 把（可能被包装的）结果委派给下一个；
- **不调 `next()` 直接返回 → 短路整条链**（即 Cordis 的 **veto**）。

```ts
ctx.on('demo/transform', async (input, next) => (await next()).toUpperCase())   // 包装
ctx.on('demo/transform', async (input, next) => input.includes('blocked') ? '** blocked **' : next())  // 短路
```

> **纪律（本仓库的standing rule）**：**只观察/注解的 listener 必须调 `next()`**；不调就是"故意短路"。忘了 `next()` 的日志 listener 会**静默吞掉下游所有人的默认行为**。

harness 用 waterfall 处理"协作插件可包装或可应答的决策"：`agent/request`（替换模型调用配置）、`approval/request`（策略代替用户应答）。

---

## 8. 组合：cordis.yml 是应用本体

```yaml
- id: greeter          # 稳定身份（loader 靠它区分"改现有"vs"删+增"）
  name: './greeter.ts'
- id: consumer
  name: './consumer.ts'
  disabled: true
```

- **`id` 至关重要**：没有 `id` 的 entry 每次读取都生成新 id，任何配置编辑后都算"删+增"→ 即使自身没改也会重挂。
- **group**：嵌套 entry 子列表，作为一个单元加载/卸载。
- **isolate**：给一个 group 独立的服务实例——两个 group 可各看一个配置不同的 `shell` provider，互不影响。
- **HMR**（`@deepseek-ai/dsh-hmr`）：监听文件，保存即"卸载旧实例（effects 全部回收）+ 加载新代码 + 重跑 apply"。

### Loader 配置
`@deepseek-ai/cordis-plugin-include` 解析 `!!js` 表达式节点。loader 对 entry 的 **`config`**（在声明的注入激活后、针对该插件 ctx 插值）和 **`disabled`**（每次挂载决策时、针对 loader ctx 插值）做插值；**其余 entry 元数据保持字面量**。用 overlay 做"环境选插件"。

---

## 9. 对照 ai-coding

| Cordis 原语 | ai-coding 现状 | 差距/可迁移 |
|---|---|---|
| 插件 + `ctx` | 无。所有组件在 `cli.py` / `server/world.py::build_world` 手工装配 | **装配是硬编码的，不可配置组合** |
| 服务 + `inject`（依赖驱动加载） | 构造器注入（`AgentLoop(engine, sessions, ...)`） | 我们的顺序由组合根手写；它由依赖自动推导 |
| 事件 + 5 种派发模式 | `core/events.py` + `LoopObserver.on_event`（仅 emit 语义） | **我们没有 waterfall（拦截/veto）、没有 serial/bail** |
| `ctx.effect()` 可逆副作用 | 无。资源靠约定式 `aclose()` | 卸载/重载无法自动回收 |
| fiber 状态机 + PENDING | 无。缺服务直接构造失败 | 它把"等待依赖"做成合法状态 |
| profile/bundle/patch | `config.local.yaml` 单文件 | 它是**分层可 patch 的插件树** |
| HMR 热替换 | 无 | — |

> **最该偷的两条**：(1) **依赖驱动加载**（`inject`）——把 `build_world` 的手写顺序换成声明式依赖；(2) **waterfall 拦截**——把 `guard.py`/审批从"调用链里的 if"升级成"可包装/可否决的事件链"。这两条不需要引入 Cordis 全套。

---

## 10. 陷阱清单（读代码时会撞到的）

1. **PENDING ≠ bug**：插件没输出先查 fiber 状态。
2. **忘了 `next()` 的 waterfall listener 会静默短路下游**。
3. **entry 无 `id` 会在每次配置编辑后重挂**。
4. **async disposer 并发**，要串行就放同一个 disposer 里 await。
5. **`declare module` 只加类型不加运行期接线**——服务仍需单独 `ctx.plugin`/`super()`。
6. **模块 resolve 失败只走 logger**，boot 早期可能丢；拼错先查。

---

## 下一步

阶段 2：心脏——`core/agent-loop`、`core/session`、`core/system-prompt`、`core/tools`、`core/scope`，产出 `02-循环与会话.md`。重点是**会话日志作为唯一真相源**与 **turn/step 生命周期**。
