/**
 * R21 §5 diagnostic: does a long CJK line leave a broken glyph at the right
 * edge?  Not a fix -- the deliverable is a verdict and a minimal repro.
 *
 * Three renderers, one width, the same text:
 *   A. ``<text>`` with the default wrap mode (word)
 *   B. ``<text wrapMode="none">``
 *   C. ``<markdown>`` (the transcript body)
 *
 * and the same three on pure ASCII and on emoji, so "is it wide-char related"
 * is answered by comparison rather than by guess.
 */
import { createTestRenderer } from "@opentui/core/testing"
import { testRender } from "@opentui/solid"

const WIDTH = 80
const ROWS = 6

const han = "汉".repeat(200)
const ascii = "abc ".repeat(200)
const emoji = "🙂".repeat(120)

async function frame(kind: "text" | "none" | "char" | "markdown", text: string) {
  const base = await createTestRenderer({ width: WIDTH, height: ROWS })
  const setup = await testRender(
    () => (
      <box width={WIDTH} height={ROWS} flexDirection="column">
        {kind === "markdown" ? (
          <markdown content={text} />
        ) : (
          <text wrapMode={kind === "text" ? undefined : kind === "char" ? "char" : "none"}>
            {text}
          </text>
        )}
      </box>
    ),
    base,
  )
  setup.resize(WIDTH, ROWS)
  await setup.flush()
  await setup.waitForVisualIdle()
  return setup.captureCharFrame()
}

function report(name: string, frame: string) {
  const lines = frame.split("\n")
  const broken = (frame.match(/\uFFFD/g) ?? []).length
  // The right-edge slice of each row: what the operator actually sees at the
  // margin, which is where the screenshot showed the damaged glyph.
  const edges = lines.map((line) => JSON.stringify(line.slice(-3))).join(" ")
  console.log(`${name}: fffd=${broken}`)
  console.log(`  edges ${edges}`)
  console.log(`  lens  ${lines.map((line) => line.length).join(",")}`)
}

for (const [name, text] of [
  ["CJK", han],
  ["ascii", ascii],
  ["emoji", emoji],
] as const) {
  for (const kind of ["text", "char", "none", "markdown"] as const) {
    report(`${name}/${kind}`, await frame(kind, text))
  }
}
