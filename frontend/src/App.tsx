import React, { useEffect, useRef } from "react";
import { useAppStore } from "./store";
import { api, createEventSource, getConfigCached } from "./api/client";
import { useRoute, getChatIdFromRoute, navigateTo } from "./router";
import { emit } from "./utils/events";
import { applySessionMetaEvent, refetchSessionMeta } from "./utils/sessionMeta";
import { LS, lsGetMap, lsGetRaw } from "./utils/localStorage";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { lazyWithRetry } from "./utils/lazyWithRetry";
import { isNativePtyFocused } from "./utils/nativePtyFocus";
import { toggleBlocksExpanded } from "./store/blocksState";
import { DelayedSkeleton } from "./components/ui/DelayedSkeleton";
import { Toast } from "./components/Toast";

function applyTheme(theme: string) {
  if (theme === "dark") {
    document.documentElement.setAttribute("data-theme", "dark");
  } else if (theme === "light") {
    document.documentElement.setAttribute("data-theme", "light");
  } else {
    document.documentElement.removeAttribute("data-theme");
    const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
    if (prefersDark) document.documentElement.setAttribute("data-theme", "dark");
  }
}

(window as Window & { __agentDecoderApplyTheme?: typeof applyTheme }).__agentDecoderApplyTheme = applyTheme;

const chatViewImport = () => import("./components/ChatView/ChatView");
const searchOverlayImport = () => import("./components/Sidebar/SearchOverlay");
const settingsModalImport = () => import("./components/Settings/SettingsModal");

// lazyWithRetry: when a code-split chunk 404s (stale hash after a rebuild),
// render a reload prompt in-place instead of crashing the whole ErrorBoundary.
const Sidebar = lazyWithRetry(() => import("./components/Sidebar/Sidebar"));
const ChatView = lazyWithRetry(chatViewImport);
const Dashboard = lazyWithRetry(() => import("./components/Dashboard/Dashboard"));
const SettingsModal = lazyWithRetry(settingsModalImport);
const SearchOverlay = lazyWithRetry(searchOverlayImport);
const HelpPopup = lazyWithRetry(() => import("./components/Sidebar/HelpPopup"));
const ShortcutsPopup = lazyWithRetry(() => import("./components/Sidebar/ShortcutsPopup"));
const FileViewer = lazyWithRetry(() => import("./components/FileViewer/FileViewer"));

export default function App() {
  // Five individual selectors instead of `useAppStore()` without a selector —
  // zustand v5 uses Object.is on the full-state return, which would re-render
  // App on every state change (including unrelated ones like a sidebar click).
  // Per-field selectors only re-render when the specific field changes.
  const isSettingsOpen = useAppStore((s) => s.isSettingsOpen);
  const isSearchOpen = useAppStore((s) => s.isSearchOpen);
  const isHelpOpen = useAppStore((s) => s.isHelpOpen);
  const isShortcutsOpen = useAppStore((s) => s.isShortcutsOpen);
  const viewingFilePath = useAppStore((s) => s.viewingFilePath);
  const activeProvider = useAppStore((s) => s.activeProvider);

  // Mirror activeProvider onto <html data-provider="..."> so CSS body/html
  // background overrides work (CSS can't select upward from a child div).
  // Mirrors the same pattern applyTheme() uses for data-theme.
  useEffect(() => {
    document.documentElement.setAttribute("data-provider", activeProvider);
  }, [activeProvider]);

  const route = useRoute();
  const chatIdFromUrl = getChatIdFromRoute(route);

  // ChatView sets activeProvider from the open session, but it UNMOUNTS on the
  // Dashboard (no chat id) so its reset never fires there. Force the provider
  // back to "claude" whenever there's no active chat, otherwise the Dashboard
  // (and the whole shell) keeps the Codex skin after leaving a Codex session.
  useEffect(() => {
    if (!chatIdFromUrl) useAppStore.getState().setActiveProvider("claude");
  }, [chatIdFromUrl]);

  // URL is the source of truth for selectedSessionId. Sync on route change
  // (covers initial mount, sidebar clicks, search clicks, browser back/forward).
  useEffect(() => {
    const current = useAppStore.getState().selectedSessionId;
    if (chatIdFromUrl !== current) {
      useAppStore.getState().selectSession(chatIdFromUrl);
    }
  }, [chatIdFromUrl]);

  useEffect(() => {
    // Cached so SettingsModal's first open paints instantly off the same
    // network round-trip used here for theme.
    getConfigCached()
      .then((cfg) => {
        applyTheme(cfg.theme);
        useAppStore.getState().setShowProviderTag(cfg.show_provider_tag ?? false);
      })
      .catch(() => {});
    // Stash host info in the store so SessionItem etc. can gate host-side
    // actions (Open in terminal, Reveal in Finder) when accessed remotely.
    api.getHostInfo()
      .then((info) => useAppStore.getState().setHostInfo(info))
      .catch(() => {});
    // Provider capabilities + runtime drivability — gates the composer, the
    // Native/Split toggle, and message edit per provider (read-only honesty).
    api.getProviders()
      .then((providers) => useAppStore.getState().setProviders(providers))
      .catch(() => {});
  }, []);

  // Preload the lazy chunks the user will likely hit first so they're ready
  // in the background before the first click — avoids a chunk-fetch delay on
  // cold first open.
  useEffect(() => {
    chatViewImport();
    searchOverlayImport();
    settingsModalImport();
  }, []);

  // One-time migration: archive / star / viewed-at used to live in
  // localStorage only — a second browser couldn't see them (the reported
  // "archive is not consistent" symptom).  On first load post-upgrade,
  // upload whatever we have to the server, then clear the LS keys so the
  // server stays the only source of truth.  Idempotent + guarded by its
  // own LS flag so it only ever runs once per browser.
  useEffect(() => {
    const MIGRATED_FLAG = "clau-decode:session-meta-migrated-v1";
    if (lsGetRaw(MIGRATED_FLAG) === "1") return;
    let archived: string[] = [];
    let starred: string[] = [];
    let viewed_at: Record<string, string> = {};
    try {
      archived = JSON.parse(localStorage.getItem(LS.ARCHIVED) ?? "[]");
    } catch { /* ignore */ }
    try {
      starred = JSON.parse(localStorage.getItem(LS.STARRED) ?? "[]");
    } catch { /* ignore */ }
    viewed_at = lsGetMap(LS.VIEWED_AT);
    const hasLegacyState =
      archived.length > 0 || starred.length > 0 || Object.keys(viewed_at).length > 0;
    if (!hasLegacyState) {
      // Nothing to migrate; still set the flag so we don't keep checking.
      localStorage.setItem(MIGRATED_FLAG, "1");
      return;
    }
    void api
      .migrateLocalStorage({ archived, starred, viewed_at })
      .then(() => {
        // Clear LS only after server confirms.  Keep the migrated flag so
        // we never re-run the migration even if the cache is later cold.
        localStorage.removeItem(LS.ARCHIVED);
        localStorage.removeItem(LS.STARRED);
        localStorage.removeItem(LS.VIEWED_AT);
        localStorage.setItem(MIGRATED_FLAG, "1");
        // Refetch the shared cache so the migrated flags appear in this
        // browser's session list without a page reload.
        void refetchSessionMeta();
      })
      .catch((err) => {
        // Leave LS intact so a retry on next reload can run.  Don't set
        // the flag — we want another shot.
        console.warn("session-meta migration failed; will retry on next load", err);
      });
  }, []);

  useEffect(() => {
    const es = createEventSource({
      onRefresh: () => emit("refresh", undefined),
      // A brand-new Codex chat just adopted its real rollout id (the live
      // driver was re-keyed in place server-side). Refresh the list, and if
      // we're viewing the placeholder, follow it to the real session.
      onSessionAdopted: ({ old, new: newId }) => {
        emit("refresh", undefined);
        if (useAppStore.getState().selectedSessionId === old) {
          navigateTo(`/chat/${newId}`);
        }
      },
      // SSE reconnected after a drop (e.g. the server restarted) — the tab
      // missed events while down, so re-sync: a "refresh" refetches the
      // session list and the open conversation (useSessionDetail listens for
      // it), and refetchSessionMeta repopulates the starred/archived/title
      // cache. Fixes the "open session looks empty after a restart" symptom.
      onReconnect: () => {
        emit("refresh", undefined);
        void refetchSessionMeta();
      },
      // Remote renames (issue #11) — fan into the same `rename` bus the
      // local SessionItem.commitRename emits on, so every view (ChatView,
      // SessionItem, ProjectGroup …) reconciles via the existing handler.
      onSessionMeta: (payload) => {
        // Rename: fan into the existing "rename" bus event for issue #11.
        if ("title" in payload) {
          emit("rename", { id: payload.id, title: payload.title ?? "" });
        }
        // Server-backed flag changes (2026-05-28): forward to the shared
        // sessionMeta cache so all hook subscribers re-render with the
        // server's new value.  The cache filters identical updates so an
        // echo of our own PUT is a no-op.
        applySessionMetaEvent(payload);
      },
      onSessionMetaBulkMigration: () => {
        // Another tab just ran the localStorage migration — refetch the
        // shared cache so our session list reflects the imported flags.
        void refetchSessionMeta();
      },
      // PTY input watchdog signals — ChatView listens to "pty-input-stalled"
      // to hide the optimistic Thinking indicator + surface an error when
      // the TUI fails to react to a submit within ~5s.
      onPtyInputAcknowledged: ({ session_id }) =>
        emit("pty-input-acknowledged", { session_id }),
      onPtyInputStalled: ({ session_id, elapsed_ms }) =>
        emit("pty-input-stalled", { session_id, elapsed_ms }),
      onPtySubmitCompleted: ({ session_id, kind, status, input_id, response_id }) =>
        emit("pty-submit-completed", { session_id, kind, status, input_id, response_id }),
      onPtyOutputChunk: ({ session_id, data_b64 }) =>
        emit("pty-output-chunk", { session_id, data_b64 }),
      onPtyNativeState: ({ session_id, state, decoded_input_safe }) =>
        emit("pty-native-state", { session_id, state, decoded_input_safe }),
      // Phase 2: /btw input captured — show the pending inline panel in
      // every connected client before the response finalizes.
      onEphemeralInputPersisted: ({ session_id, input_id, kind }) =>
        emit("ephemeral-input-persisted", { session_id, input_id, kind }),
      // Phase 2: /btw ephemeral pair captured — fan out to the event bus so
      // any mounted ChatView for this session can refetch its ephemerals.
      onEphemeralPairPersisted: ({ session_id, input_id, response_id, kind }) =>
        emit("ephemeral-pair-persisted", { session_id, input_id, response_id, kind }),
    });
    return () => es.close();
  }, []);

  // When the file viewer opens as a split pane, auto-collapse the sidebar so
  // the chat/dashboard stays readable; restore the user's prior sidebar state
  // when the file viewer closes.
  const prevSidebarRef = useRef<boolean | null>(null);
  useEffect(() => {
    if (viewingFilePath) {
      if (prevSidebarRef.current === null) {
        prevSidebarRef.current = useAppStore.getState().sidebarCollapsed;
        useAppStore.getState().setSidebarCollapsed(true);
      }
    } else if (prevSidebarRef.current !== null) {
      useAppStore.getState().setSidebarCollapsed(prevSidebarRef.current);
      prevSidebarRef.current = null;
    }
  }, [viewingFilePath]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        const s = useAppStore.getState();
        if (s.isSearchOpen || s.isSettingsOpen || s.isHelpOpen || s.isShortcutsOpen || s.viewingFilePath) {
          e.preventDefault();
          s.closeSearch();
          s.closeSettings();
          s.closeHelp();
          s.closeShortcuts();
          s.setViewingFilePath(null);
        }
        return;
      }

      const ctrl = e.metaKey || e.ctrlKey;
      if (!ctrl || e.repeat) return;

      // When the native PTY terminal is focused, every Ctrl/Cmd combo belongs
      // to claude (Ctrl+C/R/L, etc.) — don't steal them. Our shortcuts stay
      // live in the Decoded view, where no PTY is focused. (Escape, handled
      // above, still closes overlays since an open overlay holds focus.)
      if (isNativePtyFocused()) return;

      // Skip readline-style Ctrl shortcuts when focus is in a text input
      const tag = (e.target as HTMLElement)?.tagName;
      const inText = e.ctrlKey && !e.metaKey && (tag === "TEXTAREA" || tag === "INPUT");

      if (e.key === "k") {
        if (inText) return;
        e.preventDefault();
        useAppStore.getState().openSearch();
      } else if (e.key.toLowerCase() === "e" && e.metaKey && e.shiftKey) {
        e.preventDefault();
        useAppStore.getState().toggleExplorer();
      } else if (e.key.toLowerCase() === "e" && e.metaKey) {
        e.preventDefault();
        useAppStore.getState().toggleResultsExpanded();
      } else if (e.key.toLowerCase() === "b" && e.metaKey) {
        e.preventDefault();
        useAppStore.getState().toggleSidebar();
      } else if (e.key.toLowerCase() === "o") {
        // Expand/collapse all tool + thinking blocks. The scroll-preserve
        // layout side-effect lives in `useExpandPreserveAnchor` (subscribed
        // to blocksState), so we only flip the flag here.
        e.preventDefault();
        toggleBlocksExpanded();
      } else if (e.code === "Comma") {
        // Cmd/Ctrl+, (macOS preferences convention). Use e.code, not e.key:
        // with Shift held, e.key for the comma key is "<", so the old
        // `e.key === ","` check never matched. e.code is layout-independent, so
        // both Cmd+, and Cmd+Shift+, open Settings.
        e.preventDefault();
        useAppStore.getState().openSettings();
      } else if (e.code === "Slash") {
        // Cmd/Ctrl+/ (and Cmd+? via Shift) toggles the keyboard-shortcuts
        // popup. e.code is layout-independent (e.key would be "?" with Shift).
        if (inText) return;
        e.preventDefault();
        const s = useAppStore.getState();
        if (s.isShortcutsOpen) s.closeShortcuts();
        else s.openShortcuts();
      }
      // Cmd+R / Cmd+J refresh is registered in main.tsx before React mounts,
      // so it has the earliest possible registration time and best chance of
      // beating the browser's own Cmd+R handler.
    };
    // Capture phase + document target = highest priority. preventDefault here
    // wins against the browser's default reload action for Cmd+R.
    document.addEventListener("keydown", onKey, { capture: true });
    return () => document.removeEventListener("keydown", onKey, { capture: true });
  }, []);

  return (
    <div
      data-provider={activeProvider}
      style={{
        display: "flex",
        height: "100vh",
        overflow: "hidden",
        background: "var(--bg-base)",
      }}
    >
      {/* Main content in its own boundary so overlay lazy-loads never blank it. */}
      <React.Suspense fallback={<DelayedSkeleton />}>
        {/* Keep Sidebar mounted when collapsed so we don't refetch every project
            and session each time the user toggles. The Sidebar reads
            sidebarCollapsed itself and hides via display:none. */}
        <Sidebar />
        <div style={{ flex: 1, display: "flex", minWidth: 200, overflow: "hidden" }}>
          <ErrorBoundary>
            {chatIdFromUrl ? <ChatView /> : <Dashboard />}
          </ErrorBoundary>
        </div>
        {viewingFilePath && (
          <ErrorBoundary>
            <React.Suspense fallback={<DelayedSkeleton />}>
              <FileViewer />
            </React.Suspense>
          </ErrorBoundary>
        )}
      </React.Suspense>
      {/* Each overlay gets its own error boundary + Suspense — a crash or lazy
          load in one overlay never blanks the page behind it.
          fallback is `null`, NOT a skeleton: these overlays are fixed-position,
          but the skeleton is an in-flow flex element — as a flex sibling of the
          main region it briefly stole ~52px and shifted the centered content
          left on the first (un-warmed) open. The chunks are preloaded, so the
          modal pops in fast; a null fallback keeps the layout perfectly stable. */}
      {isSettingsOpen && <ErrorBoundary><React.Suspense fallback={null}><SettingsModal /></React.Suspense></ErrorBoundary>}
      {isSearchOpen && <ErrorBoundary><React.Suspense fallback={null}><SearchOverlay /></React.Suspense></ErrorBoundary>}
      {isHelpOpen && <ErrorBoundary><React.Suspense fallback={null}><HelpPopup /></React.Suspense></ErrorBoundary>}
      {isShortcutsOpen && <ErrorBoundary><React.Suspense fallback={null}><ShortcutsPopup /></React.Suspense></ErrorBoundary>}
      <Toast />
    </div>
  );
}
