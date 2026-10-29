import { describe, expect, test } from "bun:test"
import { createTestRenderer } from "@opentui/core/testing"
import { KeymapProvider } from "@opentui/keymap/solid"
import { testRender } from "@opentui/solid"
import { DialogPermissions } from "../src/component/dialog-permissions"
import { setupKeymap } from "../src/keymap"

/**
 * M-D3: the permissions panel's standing grants and the revoke affordance.
 *
 * The panel's *decision* (which command forgets a grant) is the pure
 * ``revokeGrantCommand`` and is pinned in permissions-dialog.test.ts; the key
 * path cannot be driven in this harness (the same note dialog-mcp's render
 * test carries), so this pins the words the operator reads.
 */
describe("DialogPermissions", () => {
  const frame = async (state: unknown): Promise<string> => {
    const base = await createTestRenderer({ width: 96, height: 22 })
    const setup = await testRender(
      () => (
        <KeymapProvider keymap={setupKeymap(base.renderer)}>
          <DialogPermissions
            client={{ request: async () => state } as never}
            liveSessionId="s1"
          />
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

  test("shows the standing grants and says how to revoke one", async () => {
    const text = await frame({
      policy: "confirm",
      grants: [
        { key: "write:/tmp/a.py", expiresAt: null },
        { key: "read:src", expiresAt: 1_700_000_000 },
      ],
      onceGrants: ["edit:src/b.py"],
      storePath: "/home/me/.config/ai-coding/grants.json",
    })
    expect(text).toContain("write · /tmp/a.py")
    expect(text).toContain("read · src")
    expect(text).toContain("Once  1")
    expect(text).toContain("x revoke")
    expect(text).toContain("Remembered  2")
  })
})
