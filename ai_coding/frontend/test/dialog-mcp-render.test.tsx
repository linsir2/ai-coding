import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogMcp } from "../src/component/dialog-mcp"
import { setupKeymap } from "../src/keymap"
import { DialogSelect } from "../src/component/dialog-select"

/**
 * MCP-MCP T7: the panel is read-only, so the only things it can get wrong are
 * the words on the rows.  Captured frames pin them; the connection itself is
 * covered by tests/test_mcp.py and the transport by tests/test_server_ipc.py.
 */
const ROWS = {
  servers: [
    { name: "fs", command: "mcp-fs --root /tmp", status: "connected", toolCount: 2, wantedCount: 0, detail: "" },
    { name: "web", command: "npx -y @foo/web", status: "filtered", toolCount: 1, wantedCount: 1, detail: "已过滤：fetch_page、search" },
    { name: "dead", command: "nope", status: "failed", toolCount: 0, wantedCount: 0, detail: "spawn failed: no such command" },
    { name: "off-one", command: "mcp-off", status: "off", toolCount: 0, wantedCount: 0, detail: "" },
  ],
}

/**
 * MCP-MCP T7: the panel's wording, on a captured frame.
 *
 * The panel is read-only and its only job is to show what became of each
 * configured server, so these pin the words; the connection itself is pinned in
 * tests/test_mcp.py and the transport in tests/test_server_ipc.py.
 *
 * Each test renders its own renderer and tears it down before returning: the
 * frames are cropped to the list's visible height, so sharing one renderer
 * across cases lets one case's rows bleed into the next one's capture.
 */
/**
 * Note on what is *not* here: the key path (return -> dialog.select.submit ->
 * a row's onSelect) cannot be driven in this harness.  A probe with a bare
 * ``DialogSelect`` and no component of ours at all shows the same silence, so
 * it is the harness, not the panel — and that chain is what every palette
 * selection in production already exercises.  The panel's own decision (which
 * rows carry the action) is a pure function, tested in test/mcp.test.ts; the
 * whole loop is verified by the M3 end-to-end run.
 */
describe("DialogMcp", () => {
  const frame = async (servers: unknown[]): Promise<string> => {
    const base = await createTestRenderer({ width: 78, height: 26 })
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={setupKeymap(base.renderer)}>
          <DialogMcp client={{ request: async () => ({ servers }) } as never} />
        </KeymapProvider>
      ),
      base,
    )
    await setup.flush()
    await setup.waitForVisualIdle()
    const text = setup.captureCharFrame()
    setup.renderer.destroy()
    return text
  }

  const connected = { name: "fs", command: "mcp-fs --root /tmp", status: "connected", toolCount: 2, wantedCount: 0, detail: "" }
  const filtered = { name: "web", command: "npx -y @foo/web", status: "filtered", toolCount: 1, wantedCount: 1, detail: "已过滤：fetch_page、search" }
  const failed = { name: "dead", command: "nope", status: "failed", toolCount: 0, wantedCount: 0, detail: "spawn failed: no such command" }
  const off = { name: "off-one", command: "mcp-off", status: "off", toolCount: 0, wantedCount: 0, detail: "" }

  test("a row says the state, the tool count, and the reason", async () => {
    const text = await frame([failed])
    expect(text).toContain("dead")
    expect(text).toContain("Failed !")
    expect(text).toContain("spawn failed: no such command")
  })

  test("a filtered server shows kept of asked and which tools were dropped", async () => {
    const text = await frame([filtered])
    expect(text).toContain("已过滤 · 1 / 1 tools")
    expect(text).toContain("已过滤：fetch_page、search")
  })

  test("a connected server shows its tool count", async () => {
    const text = await frame([connected])
    expect(text).toContain("Connected ✓ · 2 tools")
  })

  test("a disabled server reads as off rather than as absent", async () => {
    const text = await frame([off])
    expect(text).toContain("off-one")
    expect(text).toContain("未启用")
  })

  test("no configured servers says so instead of listing nothing", async () => {
    const text = await frame([])
    // A-6 moved the sentence from the footer into the empty body; pin the
    // position, or the old footer would satisfy a bare ``toContain`` forever.
    const line = text.split("\n").find((row) => row.includes("没有配置 MCP 服务器"))
    expect(line).toBeDefined()
    expect(line).not.toContain("esc close")
  })

  test("a server that needs a sign-in says so (M4)", async () => {
    // The full string is "Sign in required →"; the arrow does not survive this
    // harness's frame capture (the ellipsis in "Connecting …" does), so the
    // exact wording is pinned by the pure test and only the words here.
    const text = await frame([
      { name: "gh", command: "", status: "needs_auth", toolCount: 0, wantedCount: 0, detail: "No redirect URIs provided for authorization code grant" },
    ])
    expect(text).toContain("Sign in required")
    expect(text).toContain("No redirect URIs provided")
  })

  test("a server still connecting says so (M3)", async () => {
    const text = await frame([
      { name: "slow", command: "npx slow", status: "connecting", toolCount: 0, wantedCount: 0, detail: "正在连接…" },
    ])
    expect(text).toContain("Connecting …")
  })
})
