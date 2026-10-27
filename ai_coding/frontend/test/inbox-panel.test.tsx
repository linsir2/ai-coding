import { describe, expect, test } from "bun:test"
import { inboxRowLine } from "../src/component/dialog-inbox"
import { initialState, reduce } from "../src/state/session"

/**
 * D-2: the inbox table as the operator sees it.  The backend contract
 * (rows persist, edits keep the id, deletes remove) is pinned in
 * tests/test_server_ipc.py; here is the state's half: loaded replaces,
 * edited rewrites in place, deleted drops.
 */
const ROWS = [
  { id: "a1", text: "先查日志" },
  { id: "b2", text: "再看指标" },
];

describe("queued-loaded/edited/deleted", () => {
  test("loaded replaces the list with the backend's rows", () => {
    const after = reduce(initialState(), { kind: "queued-loaded", rows: ROWS })
    expect(after.queued).toEqual(ROWS)
  });

  test("edited rewrites one row and keeps its place and id", () => {
    const loaded = reduce(initialState(), { kind: "queued-loaded", rows: ROWS });
    const after = reduce(loaded, { kind: "queued-edited", id: "a1", text: "先查日志再说" });
    expect(after.queued).toEqual([
      { id: "a1", text: "先查日志再说" },
      { id: "b2", text: "再看指标" },
    ]);
  });

  test("deleted drops the row and leaves the rest alone", () => {
    const loaded = reduce(initialState(), { kind: "queued-loaded", rows: ROWS });
    const after = reduce(loaded, { kind: "queued-deleted", id: "a1" });
    expect(after.queued).toEqual([{ id: "b2", text: "再看指标" }]);
  });

  test("editing or deleting an unknown id changes nothing", () => {
    const loaded = reduce(initialState(), { kind: "queued-loaded", rows: ROWS });
    expect(reduce(loaded, { kind: "queued-edited", id: "zz", text: "x" }).queued).toEqual(ROWS);
    expect(reduce(loaded, { kind: "queued-deleted", id: "zz" }).queued).toEqual(ROWS);
  });
});

describe("a row", () => {
  test("a local-only row says it is not on disk yet", () => {
    const row = inboxRowLine({ id: "local-1", text: "断网时写的" });
    expect(row.title).toBe("断网时写的");
    expect(row.description).toContain("未落盘");
  });

  test("clips a long line rather than wrapping the panel", () => {
    const row = inboxRowLine({ id: "a1", text: "x".repeat(200) });
    expect(row.title.length).toBeLessThan(90);
    expect(row.title.endsWith("…")).toBe(true);
  });
});
