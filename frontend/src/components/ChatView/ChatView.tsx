import { useState, useEffect, useRef, useLayoutEffect, useCallback, useMemo } from "react";
import type { PointerEvent as ReactPointerEvent } from "react";
import { OverlayScrollbarsComponent } from "overlayscrollbars-react";
import type { AppConfig, NativePtyState, SessionDetail } from "../../api/types";
import { api } from "../../api/client";
import { getCached, fetchSession } from "../../api/sessionCache";
import { useAppStore } from "../../store";
import { lsGetMap, LS } from "../../utils/localStorage";
import { emit, on } from "../../utils/events";
import { SCROLLBAR_OPTIONS } from "../ScrollContainer";
import { EmptyState } from "./EmptyState";
import { ConversationHeader } from "./ConversationHeader";
import { MessageList } from "./MessageListLoader";
import { MessageListContainerCtx } from "./MessageListContainerContext";
import { ChatInputBar } from "./ChatInputBar";
import type { SubmitMeta } from "./ChatInput";
import { NativeTerminalView, type NativePtyNotice } from "./NativeTerminalView";
import { useExpandPreserveAnchor } from "./hooks/useExpandPreserveAnchor";
import { useRecaps } from "./hooks/useRecaps";
import { useSessionOwnership } from "./hooks/useSessionOwnership";
import { OwnershipBanner } from "./OwnershipBanner";
import { NativeActionBanner } from "./NativeActionBanner";
import { ProviderThemeProvider, resolveCaps } from "./ProviderThemeContext";

type ChatViewMode = "decoded" | "native" | "sbs";

// Apply the provider skin synchronously: set <html data-provider> imperatively
// (so the CSS recalc happens in the same commit as the content swap, no
// store→useEffect frame lag) AND mirror it into the store for any other
// consumers (App's mirror effect then no-ops on the same value).
function applyProviderSkin(provider: string): void {
  document.documentElement.setAttribute("data-provider", provider);
  useAppStore.getState().setActiveProvider(provider);
}

// Per-session view-mode memory: which mode each session was last viewed in.
// sessionStorage so it survives in-tab navigation, forgotten on tab close.
// A session you left in Native re-opens in Native (reusing its still-alive
// PTY, or respawning); a Decoded session stays Decoded and spawns nothing.
// We deliberately do NOT auto-switch to Native on interactive PTY state —
// only the user's explicit view choice is remembered.
function readStoredViewMode(sessionId: string): ChatViewMode | null {
  try {
    const v = sessionStorage.getItem(`agent-decoder:viewMode:${sessionId}`);
    return v === "decoded" || v === "native" || v === "sbs" ? v : null;
  } catch {
    return null;
  }
}

function storeViewMode(sessionId: string, mode: ChatViewMode): void {
  try {
    sessionStorage.setItem(`agent-decoder:viewMode:${sessionId}`, mode);
  } catch {
    // sessionStorage unavailable (private mode / disabled) — best-effort.
  }
}

function nativeStateLabel(
  state: NativePtyState | null | undefined,
  provider?: string,
): string | null {
  if (!state || state === "idle_chat_input" || state === "dead") return null;
  // The agent is actively working, not blocked — the optimistic "Thinking"
  // indicator already conveys in-flight state, so the header chip stays quiet.
  // Without this, codex's RUNNING screen (now emitted by the driver state
  // poller while a turn processes) would fall through to the default and
  // wrongly read "Native input required" for a turn that just isn't done yet.
  // (claude's classify_screen never emits these, so this is codex-specific.)
  if (
    state === "running" ||
    state === "assistant_streaming" ||
    state === "booting"
  ) {
    return null;
  }
  if (state === "slash_palette_open") return "Slash menu open";
  if (state === "login_required") {
    const providerName = provider === "codex" ? "Codex" : "Claude";
    return `${providerName} login required`;
  }
  if (state === "permission_prompt") return "Native input required";
  if (state === "ask_user_question") return "Native input required";
  if (state === "trust_prompt") return "Native input required";
  if (state === "btw_modal") return "Native input required";
  if (state === "model_selector") return "Model selector open";
  return "Native input required";
}

// The driven agent is BLOCKED waiting for the user to act in the Native pane.
// These are the states where progress is genuinely halted and the user must
// switch to Native to respond — the trigger for NativeActionBanner and the
// amber header chip. Narrower than nativeStateLabel's "Native input required"
// set: streaming/booting show that label but the agent is still working, so
// they must NOT trip a "you need to act" banner. (native-input-required-plan.md)
export function nativeNeedsAction(
  state: NativePtyState | null | undefined,
): boolean {
  return (
    state === "permission_prompt" ||
    state === "ask_user_question" ||
    state === "trust_prompt" ||
    state === "btw_modal"
  );
}

export default function ChatView() {
  const selectedSessionId = useAppStore((s) => s.selectedSessionId);
  const providersMap = useAppStore((s) => s.providers);
  const [viewMode, setViewMode] = useState<ChatViewMode>("decoded");
  // The session whose mode `viewMode` reflects. Guards the persist effect from
  // writing a stale mode under the new session's key during a switch render.
  const viewModeOwnerRef = useRef<string | null>(null);
  // Holds the full ``SessionDetail`` from /api/sessions/{id}. We narrow to
  // ``Session`` for downstream consumers that don't need the messages
  // array, but keep ``cwd_exists`` accessible here for the missing-dir
  // banner.
  const [session, setSession] = useState<SessionDetail | null>(null);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  // Effective caps for the active provider drive every interactive affordance
  // (read-only honesty). `canDriveLive` gates the composer + Native/Split
  // toggle: Claude is always live; Codex stays read-only until its caps flip
  // (4e) on a box where tmux+codex are present.
  const activeProvider = session?.provider ?? "claude";
  const caps = resolveCaps(providersMap, activeProvider);
  const canDriveLive = caps.can_send;
  // Main-turn "Thinking" indicator state. Only /btw is tracked separately:
  // it is a non-disturbing side-channel and may not produce a normal JSONL
  // end_turn. Generic slash commands remain foreground submits, but get a
  // terminal lifecycle event as a backstop when their command output lands.
  const [optimisticMainSubmit, setOptimisticMainSubmit] = useState<{
    sid: string;
    ts: number;
    content: string | null;
  } | null>(null);
  const [optimisticBtwSubmit, setOptimisticBtwSubmit] = useState<{
    sid: string;
    ts: number;
  } | null>(null);
  // Stop-button override. While stoppedSid matches the selected session,
  // force the indicator off and Stop-button back to Send. Cleared on
  // next submit (new turn) or session navigation.
  const [stoppedSid, setStoppedSid] = useState<string | null>(null);
  // Transient error chip surfaced on pty-input-stalled. Cleared on next
  // submit or session navigation.
  const [stallError, setStallError] = useState<string | null>(null);
  const [nativeState, setNativeState] = useState<{
    sessionId: string;
    state: NativePtyState;
    decodedInputSafe: boolean;
  } | null>(null);
  const [nativeHasMounted, setNativeHasMounted] = useState(false);
  // Mirror of nativeHasMounted for the decoded pane: once shown it stays
  // mounted (hidden in Native) so flipping Decoded↔Native preserves the
  // decoded scroll position instead of remounting and jumping to bottom. A
  // native-restored session doesn't mount it (no render cost) until first
  // flipped to Decoded.
  const [decodedHasMounted, setDecodedHasMounted] = useState(true);
  const [composerFocusRequest, setComposerFocusRequest] = useState(0);
  const [nativeNotice, setNativeNotice] = useState<(NativePtyNotice & { id: number }) | null>(null);
  const osRef = useRef<React.ComponentRef<typeof OverlayScrollbarsComponent>>(null);
  const scrollEl = useRef<HTMLElement | null>(null);
  const containerRef = useRef<HTMLElement | null>(null);
  const nativeNoticeId = useRef(0);

  const {
    recaps,
    recapGenerating,
    recapPromptPending,
    generateRecap,
    dismissRecapPrompt,
    dismiss,
  } = useRecaps(
    selectedSessionId,
    appConfig,
    session?.message_count ?? 0,
    // Use the conversation's own last-activity timestamp (max of message
    // timestamps) so "idle" reflects when the chat itself went quiet, not
    // when the user last clicked the sidebar entry.
    session?.updated_at ?? null,
    // Default true while detail is loading; only skip recap auto-gen
    // once we have confirmed cwd_exists is false.
    session?.cwd_exists !== false,
  );

  useEffect(() => {
    api.getConfig().then(setAppConfig).catch(() => {});
  }, []);

  // Drop the stop-override + any stale optimistic state when the user
  // navigates to a different session. Without this, optimistic from a
  // prior session would bleed into the new view.
  useEffect(() => {
    if (stoppedSid && stoppedSid !== selectedSessionId) setStoppedSid(null);
    if (optimisticMainSubmit && optimisticMainSubmit.sid !== selectedSessionId) {
      setOptimisticMainSubmit(null);
    }
    if (optimisticBtwSubmit && optimisticBtwSubmit.sid !== selectedSessionId) {
      setOptimisticBtwSubmit(null);
    }
  }, [selectedSessionId, stoppedSid, optimisticMainSubmit, optimisticBtwSubmit]);

  // Persist the current mode for its session. Declared BEFORE the restore
  // effect so on a session switch it runs first — while viewModeOwnerRef still
  // points at the OLD session — and skips, rather than writing the stale mode
  // under the new session's key.
  useEffect(() => {
    if (selectedSessionId && viewModeOwnerRef.current === selectedSessionId) {
      storeViewMode(selectedSessionId, viewMode);
    }
  }, [selectedSessionId, viewMode]);

  useEffect(() => {
    setStallError(null);
    setNativeState(null);
    // Restore the view this session was last left in. Native re-opens Native
    // (mounts the terminal → reuses its still-alive PTY, or respawns); Decoded
    // stays Decoded and unmounts the terminal, so merely reviewing a session
    // that was last in Decoded spawns nothing.
    const restored = (selectedSessionId && readStoredViewMode(selectedSessionId)) || "decoded";
    viewModeOwnerRef.current = selectedSessionId;
    setNativeHasMounted(restored !== "decoded");
    setDecodedHasMounted(restored !== "native");
    setViewMode(restored);
  }, [selectedSessionId]);

  // PTY input watchdog (backend-driven). The "stalled" event hides the
  // optimistic indicator and surfaces a clear error chip; "acknowledged"
  // is purely a liveness signal we log for debugging.
  useEffect(() => {
    if (!selectedSessionId) return;
    const offAck = on("pty-input-acknowledged", (ev) => {
      if (ev.session_id !== selectedSessionId) return;
      // eslint-disable-next-line no-console
      console.debug("[pty] input acknowledged", ev);
    });
    const offStall = on("pty-input-stalled", (ev) => {
      if (ev.session_id !== selectedSessionId) return;
      setOptimisticMainSubmit(null);
      setOptimisticBtwSubmit(null);
      setStallError(
        `Your last message didn't reach the model (no response in ${Math.round(ev.elapsed_ms / 1000)}s). Try sending again.`
      );
    });
    return () => { offAck(); offStall(); };
  }, [selectedSessionId]);

  useEffect(() => {
    if (!selectedSessionId) return;
    return on("pty-native-state", (ev) => {
      if (ev.session_id !== selectedSessionId) return;
      setNativeState({
        sessionId: ev.session_id,
        state: ev.state as NativePtyState,
        decodedInputSafe: ev.decoded_input_safe,
      });
      if (ev.state === "dead") {
        setOptimisticMainSubmit((cur) =>
          cur?.sid === selectedSessionId ? null : cur
        );
        setOptimisticBtwSubmit((cur) =>
          cur?.sid === selectedSessionId ? null : cur
        );
        setStallError(null);
      }
    });
  }, [selectedSessionId]);

  useEffect(() => {
    if (viewMode === "native" || viewMode === "sbs") setNativeHasMounted(true);
    if (viewMode === "decoded" || viewMode === "sbs") setDecodedHasMounted(true);
  }, [viewMode]);

  // Side-by-side collapses the sidebar to make room, restoring the user's prior
  // sidebar state when leaving SBS (or unmounting). Mirrors the file-viewer
  // split-pane behaviour in App.tsx.
  useEffect(() => {
    if (viewMode !== "sbs") return undefined;
    const store = useAppStore.getState();
    const priorCollapsed = store.sidebarCollapsed;
    store.setSidebarCollapsed(true);
    return () => {
      useAppStore.getState().setSidebarCollapsed(priorCollapsed);
    };
  }, [viewMode]);

  const handleViewModeChange = useCallback(
    (mode: "decoded" | "native" | "sbs") => {
      // Native/Split are the live-driving bridge; refuse them when the
      // provider isn't drivable (e.g. read-only Codex). Decoded always works.
      if (mode !== "decoded" && !canDriveLive) return;
      setViewMode(mode);
    },
    [canDriveLive],
  );

  // If driving becomes unavailable for the active session (read-only provider),
  // snap back to Decoded so we never strand the user in an empty Native pane.
  useEffect(() => {
    if (!canDriveLive && viewMode !== "decoded") setViewMode("decoded");
  }, [canDriveLive, viewMode]);

  // Cmd/Ctrl+Shift+\ cycles the view: Decoded -> Native -> Split. Registered at
  // document capture and intentionally NOT gated by isNativePtyFocused — it's the
  // keyboard escape hatch OUT of the Native view, so it must fire (and be stopped
  // before the PTY) even while the terminal is focused. Shift+\ avoids clobbering
  // the terminal's bare Ctrl+\ (SIGQUIT) for Windows/Linux users.
  useEffect(() => {
    // Cycle only modes the provider can actually enter: Decoded-only when not
    // drivable, the full Decoded→Native→Split cycle otherwise.
    const cycle: ChatViewMode[] = canDriveLive
      ? ["decoded", "native", "sbs"]
      : ["decoded"];
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || !e.shiftKey || e.repeat) return;
      if (e.code !== "Backslash") return;
      e.preventDefault();
      e.stopPropagation();
      setViewMode((mode) => cycle[(cycle.indexOf(mode) + 1) % cycle.length]);
    };
    document.addEventListener("keydown", onKey, { capture: true });
    return () => document.removeEventListener("keydown", onKey, { capture: true });
  }, [canDriveLive]);

  // Side-by-side split position: fraction of width given to the Decoded pane.
  const splitContainerRef = useRef<HTMLDivElement | null>(null);
  const [splitRatio, setSplitRatio] = useState(0.5);
  const [splitResizing, setSplitResizing] = useState(false);
  const beginSplitResize = useCallback((event: ReactPointerEvent<HTMLDivElement>) => {
    event.preventDefault();
    const container = splitContainerRef.current;
    if (!container) return;
    setSplitResizing(true);
    const onMove = (moveEvent: PointerEvent) => {
      const rect = container.getBoundingClientRect();
      if (rect.width <= 0) return;
      const ratio = (moveEvent.clientX - rect.left) / rect.width;
      setSplitRatio(Math.min(0.8, Math.max(0.2, ratio)));
    };
    const onUp = () => {
      setSplitResizing(false);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  }, []);

  useEffect(() => {
    if (!selectedSessionId) return;
    const offComplete = on("pty-submit-completed", (ev) => {
      if (ev.session_id !== selectedSessionId) return;
      if (ev.kind === "btw") {
        setOptimisticBtwSubmit((cur) =>
          cur?.sid === selectedSessionId ? null : cur
        );
        setStallError(null);
        if (ev.status === "timed_out" || ev.status === "failed") {
          const action = ev.status === "timed_out" ? "timed out" : "could not be captured";
          emit("toast", {
            message: `/btw response ${action}. Try again if you still need it.`,
            kind: "error",
          });
        }
      } else if (ev.kind === "slash") {
        setOptimisticMainSubmit((cur) =>
          cur?.sid === selectedSessionId ? null : cur
        );
        setStallError(null);
      }
    });
    const offEphemeral = on("ephemeral-pair-persisted", (ev) => {
      if (ev.session_id !== selectedSessionId) return;
      setOptimisticBtwSubmit((cur) =>
        cur?.sid === selectedSessionId ? null : cur
      );
    });
    return () => {
      offComplete();
      offEphemeral();
    };
  }, [selectedSessionId]);

  useLayoutEffect(() => {
    const instance = osRef.current?.osInstance();
    if (instance) {
      const vp = instance.elements().viewport;
      scrollEl.current = vp;
      containerRef.current = vp;
    }
  });

  // Phase-0 PTY ownership (pty-ownership-plan.md). Drives the badge in
  // ConversationHeader and the take-over banner above ChatInputBar.
  const { ownership, refetch: refetchOwnership } = useSessionOwnership(selectedSessionId);
  const foreignOwned = ownership?.status === "terminal";

  useEffect(() => {
    if (!selectedSessionId) {
      setSession(null);
      applyProviderSkin("claude");
      return;
    }
    let cancelled = false;
    const renamed = lsGetMap(LS.RENAMED)[selectedSessionId];
    const apply = (detail: SessionDetail) => {
      // Apply the provider skin imperatively in the SAME commit as the content
      // swap, so the whole shell re-themes in one paint instead of piecemeal.
      applyProviderSkin(detail.provider ?? "claude");
      setSession(renamed ? { ...detail, title: renamed } : detail);
    };
    // Route through the SAME shared sessionCache that MessageList + ChatInputBar
    // use (via useSessionDetail). Previously ChatView fetched uncached via
    // api.getSession, so the message panel (cached, fast) swapped on a different
    // clock than the header/skin/composer (uncached, slow) — the clunky
    // piecemeal switch. Now a cache hit resolves synchronously in this same
    // effect pass, so header + skin + messages + composer all flip together;
    // a miss shares ONE deduped fetch with the panel, so they still land as one.
    const cached = getCached(selectedSessionId);
    if (cached) apply(cached);
    fetchSession(selectedSessionId, api.getSession)
      .then((detail) => { if (!cancelled) apply(detail); })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [selectedSessionId]);

  useEffect(() => {
    return on("rename", ({ id, title }) => {
      if (id === selectedSessionId) {
        setSession((prev) => prev ? { ...prev, title } : prev);
      }
    });
  }, [selectedSessionId]);

  useExpandPreserveAnchor(scrollEl);

  const handleGenerateRecap = useCallback((replaceId?: number) => {
    if (!selectedSessionId) return;
    void generateRecap(selectedSessionId, replaceId).finally(() => {
      setComposerFocusRequest((value) => value + 1);
    });
  }, [generateRecap, selectedSessionId]);

  const handleNativeNotice = useCallback((notice: NativePtyNotice) => {
    nativeNoticeId.current += 1;
    setNativeNotice({ ...notice, id: nativeNoticeId.current });
  }, []);

  useEffect(() => {
    if (!nativeNotice) return undefined;
    const timer = window.setTimeout(() => {
      setNativeNotice((current) => current?.id === nativeNotice.id ? null : current);
    }, 4000);
    return () => window.clearTimeout(timer);
  }, [nativeNotice]);

  // Memoized decoded message list. In Split view the decoded pane and the
  // native terminal are mounted together, so without this the 1000s-of-message
  // list would reconcile on every pty-native-state tick (~2/s) — a ~300ms
  // main-thread block per update. The deps deliberately exclude nativeState /
  // nativeNotice so terminal-state ticks reuse this element untouched.
  const decodedPane = useMemo(() => {
    if (!selectedSessionId) return null;
    return (
      <OverlayScrollbarsComponent
        ref={osRef}
        options={SCROLLBAR_OPTIONS}
        style={{ flex: 1, minHeight: 0 }}
      >
        <MessageListContainerCtx.Provider value={containerRef}>
          <MessageList
            sessionId={selectedSessionId}
            recaps={recaps}
            recapGenerating={recapGenerating}
            recapPromptPending={recapPromptPending}
            onCreateRecap={() => handleGenerateRecap()}
            onDismissRecapPrompt={dismissRecapPrompt}
            onDismissRecap={(id) => dismiss(selectedSessionId, id)}
            onRegenerateRecap={(id) => handleGenerateRecap(id)}
            optimisticActive={
              optimisticMainSubmit?.sid === selectedSessionId
              && stoppedSid !== selectedSessionId
            }
            optimisticTimestamp={
              optimisticMainSubmit?.sid === selectedSessionId
                ? optimisticMainSubmit.ts
                : null
            }
            optimisticUserMessage={
              optimisticMainSubmit?.sid === selectedSessionId
              && optimisticMainSubmit.content
                ? {
                  content: optimisticMainSubmit.content,
                  createdAt: optimisticMainSubmit.ts,
                }
                : null
            }
            forceInactive={stoppedSid === selectedSessionId}
            onActiveChange={(active) => {
              // Turn complete (JSONL went active→done — final assistant
              // text landed). Clear the optimistic flag so the indicator
              // hides. We DON'T clear on active→true: that just means
              // streaming started and the turn is still in flight.
              if (
                !active
                && optimisticMainSubmit?.sid === selectedSessionId
              ) {
                setOptimisticMainSubmit(null);
              }
            }}
          />
        </MessageListContainerCtx.Provider>
      </OverlayScrollbarsComponent>
    );
  }, [selectedSessionId, recaps, recapGenerating, recapPromptPending, handleGenerateRecap, dismissRecapPrompt, dismiss, optimisticMainSubmit, stoppedSid, setOptimisticMainSubmit]);

  if (!selectedSessionId) {
    return (
      <div
        style={{
          flex: 1,
          display: "flex",
          flexDirection: "column",
          height: "100vh",
          overflow: "hidden",
          background: "var(--bg-base)",
        }}
      >
        <EmptyState />
      </div>
    );
  }

  return (
    <ProviderThemeProvider value={{ provider: activeProvider, caps }}>
    <div
      data-provider={activeProvider}
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        height: "100vh",
        overflow: "hidden",
        background: "var(--bg-base)",
        position: "relative",
      }}
    >
      {nativeNotice && (
        <div
          role={nativeNotice.kind === "error" ? "alert" : "status"}
          style={{
            position: "absolute",
            top: "12px",
            left: "50%",
            transform: "translateX(-50%)",
            zIndex: 10,
            maxWidth: "min(520px, calc(100% - 32px))",
            padding: "7px 11px",
            color: nativeNotice.kind === "error"
              ? "var(--tool-error-text)"
              : "var(--text-primary)",
            background: nativeNotice.kind === "error"
              ? "var(--tool-error-bg)"
              : "var(--bg-tool-block)",
            border: nativeNotice.kind === "error"
              ? "1px solid var(--tool-error-border)"
              : "1px solid var(--border-default)",
            borderRadius: "var(--radius-md)",
            boxShadow: "var(--shadow-md)",
            fontSize: "12px",
            lineHeight: 1.35,
            fontFamily: "var(--font-ui)",
            pointerEvents: "none",
          }}
        >
          {nativeNotice.text}
        </div>
      )}
      <ConversationHeader
        session={session}
        ownership={ownership}
        viewMode={viewMode}
        onViewModeChange={handleViewModeChange}
        canDriveLive={canDriveLive}
        nativeStateLabel={nativeStateLabel(nativeState?.state, session?.provider)}
        nativeNeedsAction={nativeNeedsAction(nativeState?.state)}
      />
      {session && session.cwd_exists === false && (
        <div
          role="alert"
          style={{
            padding: "10px 16px",
            margin: "8px 16px 0",
            background: "rgba(196, 122, 122, 0.10)",
            border: "1px solid rgba(196, 122, 122, 0.35)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text-primary)",
            fontFamily: "var(--font-content)",
            fontSize: "13px",
            lineHeight: 1.45,
          }}
        >
          <strong>Working directory no longer exists.</strong>{" "}
          <span style={{ color: "var(--text-secondary)" }}>
            This session's cwd <code style={{ fontFamily: "var(--font-mono)", fontSize: "12px" }}>{session.cwd ?? "(unset)"}</code> has been deleted, so new messages can't be delivered. Start a new chat from a valid directory.
          </span>
        </div>
      )}
      <div ref={splitContainerRef} style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "row" }}>
        {decodedHasMounted && (
        <div
          style={{
            // Decoded pane: full width in Decoded, a draggable fraction in
            // Side-by-side, hidden (kept MOUNTED) in Native. Staying mounted
            // preserves the decoded scroll position across Decoded↔Native
            // flips (no remount → no jump-to-bottom; that fires only on the
            // first decoded render). The decodedPane useMemo keeps React from
            // reconciling the large message list on pty_native_state ticks, so
            // keeping it mounted no longer costs the ~300ms blocks it used to.
            display: viewMode === "native" ? "none" : "flex",
            flexDirection: "column",
            minWidth: 0,
            minHeight: 0,
            ...(viewMode === "sbs"
              ? { flexGrow: 0, flexShrink: 0, flexBasis: `${splitRatio * 100}%` }
              : { flex: 1 }),
          }}
        >
          {decodedPane}
        </div>
        )}
        {viewMode === "sbs" && (
          // Same resize handle as the file-preview split pane: a thin strip that
          // is transparent until hover/drag, then highlights with the default
          // border colour. (FileViewer.tsx uses the identical pattern.)
          <div
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize split view"
            onPointerDown={beginSplitResize}
            title="Drag to resize"
            style={{
              flex: "0 0 4px",
              alignSelf: "stretch",
              cursor: "col-resize",
              background: splitResizing ? "var(--border-default)" : "transparent",
              transition: "background 0.12s",
            }}
            onMouseEnter={(e) => {
              if (!splitResizing) e.currentTarget.style.background = "var(--border-default)";
            }}
            onMouseLeave={(e) => {
              if (!splitResizing) e.currentTarget.style.background = "transparent";
            }}
          />
        )}
        {nativeHasMounted && (
          <div
            aria-hidden={viewMode === "decoded"}
            style={{
              // Native pane: full width in Native, fills the rest beside the
              // divider in Side-by-side, hidden (kept mounted) in Decoded.
              display: viewMode === "decoded" ? "none" : "flex",
              flex: 1,
              minWidth: 0,
              minHeight: 0,
            }}
          >
            <NativeTerminalView
              key={selectedSessionId}
              sessionId={selectedSessionId}
              onNotice={handleNativeNotice}
            />
          </div>
        )}
      </div>
      {stallError && (
        <div
          role="alert"
          style={{
            padding: "8px 16px",
            margin: "0 16px",
            background: "rgba(196, 122, 122, 0.12)",
            border: "1px solid rgba(196, 122, 122, 0.4)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text-primary)",
            fontFamily: "var(--font-content)",
            fontSize: "13px",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            gap: "12px",
          }}
        >
          <span>{stallError}</span>
          <button
            onClick={() => setStallError(null)}
            aria-label="Dismiss"
            style={{
              background: "none",
              border: "none",
              color: "var(--text-secondary)",
              cursor: "pointer",
              fontSize: "16px",
              padding: "0 4px",
            }}
          >
            ×
          </button>
        </div>
      )}
      {/* Prominent "input required" banner — shown in Decoded & Split (anywhere
          but Native) when the driven agent is blocked on a prompt, so a user who
          can't see the live terminal still knows to act. `!foreignOwned` because
          taking over is the only sane action while another claude holds the PTY. */}
      {viewMode !== "native" && canDriveLive && nativeState && nativeNeedsAction(nativeState.state) && !foreignOwned && (
        <NativeActionBanner
          state={nativeState.state}
          decodedInputSafe={nativeState.decodedInputSafe}
          onSwitchToNative={() => handleViewModeChange("native")}
        />
      )}
      {viewMode === "decoded" && foreignOwned && (
        <OwnershipBanner
          sessionId={selectedSessionId}
          ownership={ownership}
          onTookOver={() => {
            // Refetch immediately so the badge + disable-state clear
            // without waiting for the next 5 s poll tick.
            void refetchOwnership();
          }}
        />
      )}
      {viewMode === "decoded" && !canDriveLive && session && (
        // Read-only honesty: a non-drivable provider (e.g. Codex without its
        // caps flipped, or no tmux/codex on this box) shows an explicit note
        // instead of a composer that would misfire. Replaces the old behavior
        // where submitting on a Codex session wrongly spawned a claude PTY.
        <div
          role="note"
          style={{
            padding: "10px 16px",
            margin: "8px 16px 12px",
            background: "var(--bg-subtle)",
            border: "1px solid var(--border-subtle)",
            borderRadius: "var(--radius-sm)",
            color: "var(--text-secondary)",
            fontFamily: "var(--font-content)",
            fontSize: "13px",
            lineHeight: 1.45,
          }}
        >
          Read-only — live driving isn’t available for{" "}
          {activeProvider === "codex" ? "Codex" : activeProvider} sessions
          {providersMap?.[activeProvider]?.availability?.reason
            ? ` (${providersMap[activeProvider].availability.reason})`
            : ""}
          . Use “Open in terminal” to continue this session in its own CLI.
        </div>
      )}
      {viewMode === "decoded" && canDriveLive && (
        <ChatInputBar
          sessionId={selectedSessionId}
          session={session}
          defaultPermissionMode={appConfig?.claude_default_permission_mode ?? "default"}
          chatSendShortcut={appConfig?.chat_send_shortcut ?? "enter"}
          focusRequestKey={composerFocusRequest}
          flushTop={foreignOwned}
          forceInactive={
            stoppedSid === selectedSessionId
            || foreignOwned
            || (stallError !== null && optimisticMainSubmit?.sid !== selectedSessionId)
          }
          disableInput={foreignOwned}
          optimisticActive={
            optimisticMainSubmit?.sid === selectedSessionId
            && stoppedSid !== selectedSessionId
          }
          btwCaptureActive={optimisticBtwSubmit?.sid === selectedSessionId}
          onSubmitStart={(meta?: SubmitMeta) => {
            const kind = meta?.kind ?? "message";
            const ts = Date.now();
            if (kind === "message" || kind === "slash") {
              // Capture wall-clock at submit. The indicator's counter uses
              // this as its base — "current-turn duration" semantics
              // (matching claude's own TUI timer).
              setOptimisticMainSubmit({
                sid: selectedSessionId,
                ts,
                content: kind === "message" ? meta?.content ?? null : null,
              });
              // Starting a new turn cancels any prior stop-override.
              if (stoppedSid === selectedSessionId) setStoppedSid(null);
            } else if (kind === "btw") {
              setOptimisticBtwSubmit({ sid: selectedSessionId, ts });
            }
            // Clear any prior stall error; the new submit will produce
            // its own ack/stall verdict via the backend watchdog.
            setStallError(null);
          }}
          onSubmitFailed={(meta?: SubmitMeta) => {
            const kind = meta?.kind ?? "message";
            if (kind === "btw") {
              setOptimisticBtwSubmit((cur) =>
                cur?.sid === selectedSessionId ? null : cur
              );
            } else {
              setOptimisticMainSubmit((cur) =>
                cur?.sid === selectedSessionId ? null : cur
              );
            }
          }}
          onStopFired={() => setStoppedSid(selectedSessionId)}
        />
      )}
    </div>
    </ProviderThemeProvider>
  );
}
