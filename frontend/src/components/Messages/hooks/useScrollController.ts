import { useEffect, useRef } from "react";
import type { SessionDetail } from "../../../api/types";
import { SCROLL } from "../../../config/ui";

type ScrollRef = { readonly current: HTMLElement | null };

// Survives MessageList's per-session remount (ChatView renders it with
// key={sessionId}) the same way api/sessionCache.ts survives it — module
// state, not React state, since a per-session scroll position needs no
// reactivity, just to outlive the component that recorded it.
const scrollPositionsBySession = new Map<string, { top: number; height: number }>();

// "Near bottom" (within ~one viewport) — the loose test for "does a saved
// position even need restoring, or would the reader land there anyway".
function isNearBottom(top: number, height: number, clientHeight: number): boolean {
  return height - top - clientHeight < clientHeight;
}

// Write scrollTop = bottom, flagging it as ours (programmaticRef) ONLY if the
// write will actually move the view. A write that lands where we already are
// fires no `scroll`/`scrollend`, so nothing would ever clear the flag — it
// stays stuck true and onScroll then ignores the reader's next scroll-up
// (never unpinning), so the following streamed update snaps them back to the
// bottom. `>= max - 1` also covers browsers clamping scrollTop to
// scrollHeight - clientHeight.
function snapToBottom(container: HTMLElement, programmaticRef: { current: boolean }): void {
  const max = container.scrollHeight - container.clientHeight;
  if (container.scrollTop >= max - 1) return;
  programmaticRef.current = true;
  container.scrollTop = container.scrollHeight;
}

/**
 * Single owner of this session's scroll container: who is "pinned" to the
 * bottom, whose move is in flight, and where the reader left off last time.
 *
 * This replaces three previously-independent hooks (useScrollPositionMemory,
 * useSnapToBottom, useSearchScroll) that each kept their own private
 * "am I at the bottom" / "is a scroll in flight" state and fought over the
 * same scrollTop via timers and DOM-attribute side-channels — the git log
 * for those files has 11 separate "fix(scroll)" commits, each patching a
 * race between two of them. Centralizing the pin state and the single
 * imperative scroll-write path removes the need to guess at another
 * effect's timing at all.
 */
export function useScrollController(
  containerRef: ScrollRef | null,
  sessionId: string,
  detail: SessionDetail | null,
  msgToAnchorRef: React.RefObject<Map<string, string>>,
  pendingScrollMessageId: string | null,
  setPendingScrollMessageId: (id: string | null) => void,
  optimisticActive: boolean = false,
): void {
  // Should this view auto-follow content growth (streaming, first load)?
  // Direction-based, NOT a distance threshold: any deliberate upward scroll
  // stops following; only returning to the bottom re-engages it. A distance
  // threshold alone re-snaps on scrolls smaller than the threshold, which
  // reads as "I can't scroll away from a live session" to the user.
  const pinnedRef = useRef(true);
  // True while THIS controller is writing scrollTop (search navigation,
  // resize/glitch correction, streaming follow). Gates the raw scroll
  // listener so our own writes are never misread as user intent, and gates
  // the resize/mutation observers so they don't fight an in-flight move.
  // Cleared by the browser's native `scrollend` event — which fires once a
  // scroll (smooth or instant) truly finishes — instead of a fixed-duration
  // guess. A guessed duration is exactly what kept failing here: it has to
  // outlast whatever the move turns out to take, and different moves
  // (search scrollIntoView vs. an instant bottom-snap) take different
  // amounts of time for reasons this hook can't predict from the outside.
  const programmaticRef = useRef(false);
  const scrolledSessionRef = useRef<string | null>(null);

  // Listeners + observers: attached once per session, not re-attached on
  // every `detail` update (streaming would otherwise tear down and rebuild
  // them on every message).
  useEffect(() => {
    const container = containerRef?.current;
    if (!container) return;

    const scrollTo = (top: number, behavior: ScrollBehavior = "auto") => {
      programmaticRef.current = true;
      // jsdom (unit tests) doesn't implement scrollTo — fall back to a
      // direct assignment so behavior is equivalent minus the animation.
      if (typeof container.scrollTo === "function") {
        container.scrollTo({ top, behavior });
      } else {
        container.scrollTop = top;
      }
    };
    const scrollToBottomNow = () => snapToBottom(container, programmaticRef);

    let pendingRaf = 0;
    const resettle = () => {
      pendingRaf = 0;
      if (programmaticRef.current) return;
      if (pinnedRef.current) {
        scrollToBottomNow();
        return;
      }
      const last = scrollPositionsBySession.get(sessionId);
      if (!last) return;
      if (Math.abs(container.scrollTop - last.top) > 1) {
        scrollTo(last.top);
      }
    };
    const scheduleResettle = () => {
      if (pendingRaf) return;
      pendingRaf = requestAnimationFrame(resettle);
    };

    // Was a scrollTop change caused by real input, vs. some non-user reset
    // (e.g. a scrollbar library re-render zeroing scrollTop)? Both produce
    // an identical `scroll` event, so this can't be told apart from the
    // scroll signal alone — it has to come from input.
    //
    // Armed on `window` in the capture phase, not just on `container`:
    // OverlayScrollbars renders its scrollbar track/thumb as a sibling of
    // the viewport, so a scrollbar-thumb drag's `pointerdown` never reaches
    // `container` — an el-scoped listener would miss it entirely, treating
    // a deliberate drag as a non-user reset.
    //
    // Every `scroll` event that lands while intent is armed re-arms it, so
    // trackpad momentum — which keeps emitting `scroll` long after the one
    // real `wheel` event that started it — stays "user" for the gesture's
    // full duration instead of going stale mid-flick.
    const SCROLL_INTENT_DECAY_MS = 200;
    let scrollIntentActive = false;
    let decayTimer: ReturnType<typeof setTimeout> | undefined;
    const armScrollIntent = () => {
      scrollIntentActive = true;
      if (decayTimer !== undefined) clearTimeout(decayTimer);
      decayTimer = setTimeout(() => {
        scrollIntentActive = false;
      }, SCROLL_INTENT_DECAY_MS);
    };

    let lastTop = container.scrollTop;
    const onScroll = () => {
      if (scrollIntentActive) armScrollIntent(); // momentum: keep it warm

      const top = container.scrollTop;
      const height = container.scrollHeight;
      const clientHeight = container.clientHeight;

      if (programmaticRef.current) {
        // Our own move (or still settling from it) — just track position.
        scrollPositionsBySession.set(sessionId, { top, height });
        lastTop = top;
        return;
      }

      const atBottomNow = height - top - clientHeight <= SCROLL.SNAP_THRESHOLD_PX;

      if (!scrollIntentActive) {
        // No evidence this was the user — could be an external library
        // resetting scrollTop (OverlayScrollbars, a resize side-effect).
        // Landing back at the bottom is always honored passively; anything
        // else is undone rather than recorded as the reader's new position
        // or read as intent to stop following.
        if (atBottomNow) {
          pinnedRef.current = true;
        } else {
          scheduleResettle();
          lastTop = top;
          return;
        }
      } else if (top < lastTop - 1) {
        pinnedRef.current = false; // moved up → reading history
      } else if (atBottomNow) {
        pinnedRef.current = true; // returned to bottom → follow again
      }
      lastTop = top;
      scrollPositionsBySession.set(sessionId, { top, height });
    };
    const onScrollEnd = () => {
      programmaticRef.current = false;
    };
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY < 0) pinnedRef.current = false;
    };

    container.addEventListener("scroll", onScroll, { passive: true });
    container.addEventListener("scrollend", onScrollEnd, { passive: true });
    container.addEventListener("wheel", onWheel, { passive: true });
    window.addEventListener("wheel", armScrollIntent, { passive: true, capture: true });
    window.addEventListener("touchstart", armScrollIntent, { passive: true, capture: true });
    window.addEventListener("touchmove", armScrollIntent, { passive: true, capture: true });
    window.addEventListener("pointerdown", armScrollIntent, { capture: true });
    window.addEventListener("keydown", armScrollIntent, { capture: true });

    const ro = new ResizeObserver(scheduleResettle);
    const mo = new MutationObserver(scheduleResettle);
    if (container instanceof Node) {
      ro.observe(container);
      mo.observe(container, { attributes: true, childList: true, subtree: true });
    }

    return () => {
      container.removeEventListener("scroll", onScroll);
      container.removeEventListener("scrollend", onScrollEnd);
      container.removeEventListener("wheel", onWheel);
      window.removeEventListener("wheel", armScrollIntent, { capture: true });
      window.removeEventListener("touchstart", armScrollIntent, { capture: true });
      window.removeEventListener("touchmove", armScrollIntent, { capture: true });
      window.removeEventListener("pointerdown", armScrollIntent, { capture: true });
      window.removeEventListener("keydown", armScrollIntent, { capture: true });
      if (pendingRaf) cancelAnimationFrame(pendingRaf);
      if (decayTimer !== undefined) clearTimeout(decayTimer);
      ro.disconnect();
      mo.disconnect();
    };
  }, [containerRef, sessionId]);

  // Session mount: restore the last read position, or stick to the bottom
  // through the initial reflow (syntax highlighting, image decode) for a
  // bounded window. Keyed on a stable derived flag instead of `detail`
  // itself so streaming updates and SSE refreshes don't tear this down
  // mid-flight.
  const hasCurrentDetail = !!detail && detail.id === sessionId;
  useEffect(() => {
    if (!hasCurrentDetail) return;
    if (scrolledSessionRef.current === sessionId) return;
    scrolledSessionRef.current = sessionId;
    if (pendingScrollMessageId) {
      // The search-navigation effect below will handle placing the reader.
      pinnedRef.current = false;
      return;
    }

    const container = containerRef?.current;
    if (!container) return;

    const saved = scrollPositionsBySession.get(sessionId);
    if (saved && !isNearBottom(saved.top, saved.height, container.clientHeight)) {
      pinnedRef.current = false;
      programmaticRef.current = true;
      container.scrollTop = saved.top;
      return;
    }

    pinnedRef.current = true;
    snapToBottom(container, programmaticRef);

    const inner = container.firstElementChild;
    let ro: ResizeObserver | null = null;
    if (inner) {
      ro = new ResizeObserver(() => {
        if (pinnedRef.current) snapToBottom(container, programmaticRef);
      });
      ro.observe(inner);
    }
    const timeout = setTimeout(() => {
      ro?.disconnect();
      ro = null;
    }, SCROLL.SNAP_TIMEOUT_MS);

    return () => {
      ro?.disconnect();
      clearTimeout(timeout);
    };
  }, [sessionId, hasCurrentDetail, pendingScrollMessageId, containerRef]);

  // Streaming auto-follow: snap to bottom on detail updates, but only while
  // pinned (see pinnedRef's direction-based tracking above).
  useEffect(() => {
    if (!detail || detail.id !== sessionId) return;
    if (scrolledSessionRef.current !== sessionId) return;
    if (!pinnedRef.current) return;
    const container = containerRef?.current;
    if (!container) return;
    snapToBottom(container, programmaticRef);
  }, [detail, sessionId, containerRef]);

  // Snap when the optimistic "thinking" indicator first appears. Bypasses
  // the pin gate: the user just hit Send, so they expect to see it
  // regardless of prior scroll position.
  useEffect(() => {
    if (!optimisticActive) return;
    if (scrolledSessionRef.current !== sessionId) return;
    const container = containerRef?.current;
    if (!container) return;
    snapToBottom(container, programmaticRef);
    pinnedRef.current = true;
  }, [optimisticActive, sessionId, containerRef]);

  // Search navigation: scroll to (and briefly highlight) a search result.
  // Explicitly unpins — arriving here is the same intent as a manual
  // scroll-up, so the observers above leave the landed position alone
  // without needing to know anything about search specifically.
  useEffect(() => {
    if (!pendingScrollMessageId || !detail || detail.id !== sessionId) return;
    const anchorId = msgToAnchorRef.current?.get(pendingScrollMessageId) ?? pendingScrollMessageId;
    const el = document.querySelector(`[data-message-id="${anchorId}"]`) as HTMLElement | null;
    if (!el) return;
    setPendingScrollMessageId(null);
    const container = containerRef?.current;
    if (container) {
      const containerRect = container.getBoundingClientRect();
      const elRect = el.getBoundingClientRect();
      const top = container.scrollTop + elRect.top - containerRect.top - SCROLL.NEAR_BOTTOM_PX;
      pinnedRef.current = false;
      programmaticRef.current = true;
      if (typeof container.scrollTo === "function") {
        container.scrollTo({ top: top < 0 ? 0 : top, behavior: "smooth" });
      } else {
        container.scrollTop = top < 0 ? 0 : top;
      }
    }
    el.setAttribute("data-highlight", "1");
    const t = setTimeout(() => el.removeAttribute("data-highlight"), SCROLL.SEARCH_HIGHLIGHT_MS);
    return () => clearTimeout(t);
  }, [detail, sessionId, pendingScrollMessageId, setPendingScrollMessageId, containerRef, msgToAnchorRef]);
}
