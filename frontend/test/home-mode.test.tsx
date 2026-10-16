/**
 * 20 §批次1（冷启动）：Home 自己携带模式切换。
 *
 * Session 路由（以及它身上的 shift+tab 键位层）只在首次提交后才挂载——
 * 用户冷启动想先切 PLAN 时，键位恰好不存在（会话 24d7109b + 仪器实测的
 * 根因）。 本测试用 runCommand 驱动循环（渲染器夹具无法投递按键；stroke
 * 本身由 keymap-shift-tab.test.tsx 钉住），并断言绑定确实注册在 Home 层。
 */

import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogProvider } from "../src/dialog/context"
import { Home } from "../src/routes/home"
import { setupKeymap } from "../src/keymap"

describe("Home mode row (cold start)", () => {
  test("the binding is registered and the cycle flips the row Build <-> PLAN", async () => {
    const base = await createTestRenderer({ width: 100, height: 40 })
    const keymap = setupKeymap(base.renderer)
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={keymap}>
          <DialogProvider renderer={base.renderer}>
            <Home
              workspace="/tmp/ws"
              version="0.1.0"
              model="m"
              provider="p"
              branch=""
              onSubmit={() => {}}
              client={{} as never}
              skills={[]}
              files={[]}
              commands={["plan", "help"]}
            />
          </DialogProvider>
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()

    expect(keymap.getActiveKeys().map((key) => key.display)).toContain("shift+tab")
    const frameOf = () => setup.captureCharFrame().split("\n").join(" ")
    expect(frameOf()).toContain("Build · p · m")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("PLAN · p · m")

    keymap.runCommand("home.mode.cycle")
    await setup.flush()
    await setup.waitForVisualIdle()
    expect(frameOf()).toContain("Build · p · m")

    setup.renderer.destroy()
  })
})
