// @vitest-environment node
import { describe, it, expect } from "vitest";
import { createNativeTerminal, type NativeTerminalOptions } from "../xtermTerminal";

const w = (t: ReturnType<typeof createNativeTerminal>, d: string) =>
  new Promise<void>((r) => t.write(d, () => r()));

async function scrolledTerminal() {
  const t = createNativeTerminal({
    cols: 120,
    rows: 30,
    scrollback: 5000,
    allowProposedApi: true,
  } as NativeTerminalOptions);
  let s = "";
  for (let i = 0; i < 400; i++) s += `line ${i}\r\n`;
  await w(t, s);
  return t;
}
const pos = (t: ReturnType<typeof createNativeTerminal>) => {
  const b = t.buffer.active;
  return { y: b.viewportY, base: b.baseY, scrollback: b.length - t.rows };
};

// Regression: xterm honors ESC[3J (erase scrollback) by wiping history AND
// resetting the viewport to the bottom. A TUI that emits a full clear on a tall
// repaint therefore yanks a reader who scrolled up back down "a few moments"
// later and destroys what they were reading.
describe("ESC[3J (erase scrollback) while the reader is scrolled up", () => {
  it("is ignored: position and scrollback are preserved", async () => {
    const t = await scrolledTerminal();
    t.scrollLines(-100);
    const before = pos(t);
    await w(t, "\x1b[2J\x1b[3J\x1b[Hrepaint");
    const after = pos(t);
    expect(after.y).toBe(before.y);
    expect(after.scrollback).toBeGreaterThanOrEqual(before.scrollback);
    expect(after.y).toBeLessThan(after.base); // still not at the bottom
  });

  it("is ignored even when the sequence is split across two writes", async () => {
    const t = await scrolledTerminal();
    t.scrollLines(-100);
    const before = pos(t);
    await w(t, "\x1b[3");
    await w(t, "Jrepaint");
    expect(pos(t).y).toBe(before.y);
    expect(pos(t).scrollback).toBeGreaterThanOrEqual(before.scrollback);
  });

  it("still clears scrollback when the reader is at the bottom (faithful /clear)", async () => {
    const t = await scrolledTerminal(); // at the bottom: never scrolled up
    await w(t, "\x1b[2J\x1b[3J\x1b[Hfresh");
    expect(pos(t).scrollback).toBe(0);
  });

  it("does not affect other erase sequences", async () => {
    const t = await scrolledTerminal();
    t.scrollLines(-100);
    const before = pos(t);
    await w(t, "\x1b[2J\x1b[Hrepaint");
    expect(pos(t).y).toBe(before.y);
  });
});
