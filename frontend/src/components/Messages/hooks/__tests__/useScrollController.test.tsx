import { useRef } from "react";
import { fireEvent, render } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { useScrollController } from "../useScrollController";
import type { SessionDetail } from "../../../../api/types";

function detail(id: string): SessionDetail {
  return { id } as SessionDetail;
}

function Harness({
  el,
  sessionId,
  detail: d,
  pendingScrollMessageId = null,
  setPendingScrollMessageId = () => {},
  optimisticActive = false,
}: {
  el: HTMLElement;
  sessionId: string;
  detail: SessionDetail | null;
  pendingScrollMessageId?: string | null;
  setPendingScrollMessageId?: (id: string | null) => void;
  optimisticActive?: boolean;
}) {
  const containerRef = useRef<HTMLElement | null>(el);
  const msgToAnchorRef = useRef(new Map<string, string>());
  useScrollController(
    containerRef,
    sessionId,
    d,
    msgToAnchorRef,
    pendingScrollMessageId,
    setPendingScrollMessageId,
    optimisticActive,
  );
  return null;
}

function makeContainer(scrollHeight: number, clientHeight: number) {
  const el = document.createElement("div");
  const inner = document.createElement("div");
  el.appendChild(inner);
  Object.defineProperty(el, "scrollHeight", { value: scrollHeight, configurable: true });
  Object.defineProperty(el, "clientHeight", { value: clientHeight, configurable: true });
  return el;
}

// jsdom doesn't fire `scroll`/`scrollend` on its own — real Chrome does,
// for both instant and smooth scrollTop changes, which is exactly what
// programmaticRef relies on to know a controller-driven move has finished.
// Tests simulate that settling explicitly instead of guessing a duration.
async function settle(el: HTMLElement) {
  fireEvent.scroll(el);
  fireEvent(el, new Event("scrollend"));
  await new Promise((r) => requestAnimationFrame(() => r(null)));
}

describe("useScrollController", () => {
  let resizeCallbacks: Array<() => void>;
  let mutationCallbacks: Array<() => void>;
  // scrollPositionsBySession is module-level (survives real component
  // remounts by design) — give each test its own session id so they can't
  // read a position a previous test saved under the same id.
  let sid = 0;
  const nextSessionId = () => `test-session-${++sid}`;

  beforeEach(() => {
    resizeCallbacks = [];
    mutationCallbacks = [];
    vi.stubGlobal(
      "ResizeObserver",
      class {
        cb: () => void;
        constructor(callback: () => void) {
          this.cb = callback;
          resizeCallbacks.push(callback);
        }
        observe = vi.fn();
        disconnect = vi.fn();
      },
    );
    vi.stubGlobal(
      "MutationObserver",
      class {
        constructor(callback: () => void) {
          mutationCallbacks.push(callback);
        }
        observe = vi.fn();
        disconnect = vi.fn();
      },
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("snaps to bottom on first load of a session with no saved position", async () => {
    const s1 = nextSessionId();
    const el = makeContainer(1200, 300);
    render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);
    expect(el.scrollTop).toBe(1200);
  });

  it("restores a saved read position on session switch instead of forcing bottom", async () => {
    const s1 = nextSessionId();
    const s2 = nextSessionId();
    const el = makeContainer(1200, 300);
    const { rerender } = render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);

    // Reader scrolls up and away — this should be saved as s1's position.
    // A real wheel event has to precede a big jump like this one, or the
    // anti-glitch guard (a sudden jump away from "near bottom" with no
    // input evidence) reads it as an external reset instead of intent.
    fireEvent.wheel(window, { deltaY: -10 });
    el.scrollTop = 100;
    fireEvent.scroll(el);
    fireEvent(el, new Event("scrollend"));

    // Switch to a different session, then back to s1.
    rerender(<Harness el={el} sessionId={s2} detail={detail(s2)} />);
    await settle(el);
    rerender(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);

    expect(el.scrollTop).toBe(100);
  });

  it("stops following on a small scroll-up, and only re-follows once back at the bottom", async () => {
    // Regression: a distance-threshold-based follow check re-snaps on any
    // scroll smaller than the threshold. Must be direction-based instead.
    const s1 = nextSessionId();
    const el = makeContainer(2000, 300);
    const { rerender } = render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);
    expect(el.scrollTop).toBe(2000);

    fireEvent.wheel(window, { deltaY: -10 }); // a real scroll gesture is always preceded by input
    el.scrollTop = 1950; // scrolled up 50px — still "near bottom" by a loose test
    fireEvent.scroll(el);

    // Content grows (new streamed detail) while the reader is mid-read.
    rerender(<Harness el={el} sessionId={s1} detail={{ id: s1 } as SessionDetail} />);
    expect(el.scrollTop).toBe(1950); // must NOT have been re-snapped to 2000
  });

  it("does not snap back after a scroll-up that follows a no-op follow-write at the bottom", async () => {
    // Regression: a streaming follow-write while already at the bottom changes
    // nothing, so the browser fires no scroll/scrollend to clear
    // programmaticRef. Left stuck, onScroll ignored the reader's next
    // scroll-up (keyboard / touch / scrollbar drag — no wheel event to unpin
    // via onWheel), pinned stayed true, and the NEXT streamed update snapped
    // them back to the bottom "a few moments" later.
    const s1 = nextSessionId();
    const el = makeContainer(2000, 300);
    const { rerender } = render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);
    expect(el.scrollTop).toBe(2000);

    // Streamed update while at the bottom: follow-write is a no-op, so
    // (like real Chrome) NO scroll/scrollend event is fired afterwards.
    rerender(<Harness el={el} sessionId={s1} detail={{ id: s1 } as SessionDetail} />);

    fireEvent.keyDown(window, { key: "PageUp" }); // input that is not a wheel
    el.scrollTop = 1500;
    fireEvent.scroll(el);

    // Next streamed chunk arrives.
    rerender(<Harness el={el} sessionId={s1} detail={{ id: s1 } as SessionDetail} />);
    expect(el.scrollTop).toBe(1500); // must NOT snap back to the bottom
  });

  it("re-engages follow once the reader scrolls back to the bottom", async () => {
    const s1 = nextSessionId();
    const el = makeContainer(2000, 300);
    const { rerender } = render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);

    fireEvent.wheel(window, { deltaY: -10 });
    el.scrollTop = 1000; // scrolled well up
    fireEvent.scroll(el);
    el.scrollTop = 1700; // back within SNAP_THRESHOLD_PX of bottom (2000-300=1700)
    fireEvent.scroll(el);

    // New content streams in (grows the scrollable height), then the update lands.
    Object.defineProperty(el, "scrollHeight", { value: 2100, configurable: true });
    rerender(<Harness el={el} sessionId={s1} detail={{ id: s1 } as SessionDetail} />);
    expect(el.scrollTop).toBe(2100); // re-engaged follow snapped to the new bottom
  });

  it("does not snap back to bottom when a search result scrolls the reader away from it", async () => {
    // The bug this whole consolidation exists to kill: a search-driven
    // scroll away from the bottom must hold, through both the scroll
    // animation and any DOM mutation the highlight causes afterward.
    const s1 = nextSessionId();
    const el = makeContainer(2000, 300);
    const target = document.createElement("div");
    target.setAttribute("data-message-id", "msg-1");
    el.firstElementChild!.appendChild(target);
    document.body.appendChild(el);
    try {
      const setPending = vi.fn();
      const { rerender } = render(
        <Harness el={el} sessionId={s1} detail={detail(s1)} pendingScrollMessageId={null} setPendingScrollMessageId={setPending} />,
      );
      await settle(el);
      expect(el.scrollTop).toBe(2000);

      rerender(
        <Harness el={el} sessionId={s1} detail={detail(s1)} pendingScrollMessageId="msg-1" setPendingScrollMessageId={setPending} />,
      );

      expect(setPending).toHaveBeenCalledWith(null);
      expect(el.scrollTop).not.toBe(2000); // scrollTo() was invoked, landing away from bottom
      expect(target.getAttribute("data-highlight")).toBe("1");

      // The (mocked) scrollTo doesn't actually move scrollTop in jsdom, so
      // simulate the animation's own scroll events landing away from bottom,
      // then the browser reporting it finished.
      el.scrollTop = 50;
      fireEvent.scroll(el);
      fireEvent(el, new Event("scrollend"));

      // A DOM mutation fires (e.g. the highlight attribute being removed
      // later, or unrelated streamed content elsewhere in the list).
      mutationCallbacks.forEach((cb) => cb());
      await settle(el);

      expect(el.scrollTop).toBe(50); // held — not yanked back to bottom
    } finally {
      document.body.removeChild(el);
    }
  });

  it("restores position on a container resize while not pinned (sidebar toggle)", async () => {
    // Uses fake timers so the scroll-intent decay window (200ms) genuinely
    // elapses between the deliberate scroll-up and the later, unrelated
    // resize glitch — without that gap, the reset's own scroll event would
    // still read as "evidenced" and overwrite the saved position before the
    // resize-triggered resettle() gets a chance to restore it.
    vi.useFakeTimers();
    const s1 = nextSessionId();
    const el = makeContainer(2000, 300);
    try {
      render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
      fireEvent.scroll(el);
      fireEvent(el, new Event("scrollend"));

      fireEvent.wheel(window, { deltaY: -10 }); // arm intent for the deliberate scroll-up below
      el.scrollTop = 500;
      fireEvent.scroll(el);
      vi.advanceTimersByTime(250); // let scroll-intent decay before the unrelated resize

      el.scrollTop = 0; // simulate a resize resetting scrollTop
      resizeCallbacks.forEach((cb) => cb());
      fireEvent.scroll(el); // the reset's own (unevidenced) scroll event
      vi.advanceTimersByTime(20); // flush the rAF-coalesced resettle

      expect(el.scrollTop).toBe(500);
    } finally {
      vi.useRealTimers();
    }
  });

  it("re-snaps to bottom on a container resize while pinned", async () => {
    const s1 = nextSessionId();
    const el = makeContainer(1200, 300);
    render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el);

    el.scrollTop = 0;
    Object.defineProperty(el, "scrollHeight", { value: 1500, configurable: true });
    resizeCallbacks.forEach((cb) => cb());
    await settle(el);

    expect(el.scrollTop).toBe(1500);
  });

  it("undoes a non-user scrollTop reset without treating it as intent to stop following", async () => {
    const s1 = nextSessionId();
    const el = makeContainer(1200, 300);
    render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
    await settle(el); // parked at 1200, pinned

    el.scrollTop = 0; // a library resets it with no real input evidence
    fireEvent.scroll(el);
    mutationCallbacks.forEach((cb) => cb());

    await settle(el);
    expect(el.scrollTop).toBe(1200);
  });

  it("treats a scrollbar-thumb drag (pointerdown on a sibling) as real intent", async () => {
    const s1 = nextSessionId();
    const el = makeContainer(1200, 300);
    document.body.appendChild(el);
    const scrollbarThumb = document.createElement("div");
    document.body.appendChild(scrollbarThumb);
    try {
      render(<Harness el={el} sessionId={s1} detail={detail(s1)} />);
      await settle(el);

      el.scrollTop = 900; // near bottom
      fireEvent.scroll(el);
      fireEvent.pointerDown(scrollbarThumb); // drag starts on the thumb, not el
      el.scrollTop = 200; // dragged far from the bottom
      fireEvent.scroll(el);

      await settle(el);
      expect(el.scrollTop).toBe(200); // held — not undone as a "glitch"
    } finally {
      document.body.removeChild(el);
      document.body.removeChild(scrollbarThumb);
    }
  });

  it("snaps to bottom when the optimistic indicator appears, regardless of pin state", async () => {
    const s1 = nextSessionId();
    const el = makeContainer(1200, 300);
    const { rerender } = render(
      <Harness el={el} sessionId={s1} detail={detail(s1)} optimisticActive={false} />,
    );
    await settle(el);

    el.scrollTop = 0; // reading history, not pinned
    fireEvent.scroll(el);

    rerender(<Harness el={el} sessionId={s1} detail={detail(s1)} optimisticActive={true} />);
    expect(el.scrollTop).toBe(1200);
  });
});
