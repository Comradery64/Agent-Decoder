import { useState, useRef, useEffect, useCallback } from "react";
import type { RunnerStatus, Session } from "../../api/types";
import { api } from "../../api/client";
import { useAppStore } from "../../store";
import { prefetch } from "../../api/sessionCache";
import { lsGetMap, lsPutMap, LS } from "../../utils/localStorage";
import { emit, on } from "../../utils/events";
import {
  useArchivedSet,
  useStarredSet,
  useViewedAt,
} from "../../utils/sessionMeta";
import { formatRelativeBucket } from "../../utils/formatRelative";
import { UI } from "../../config/ui";
import { IconStar, IconRename, IconArchive, IconCopy, IconTerminal, IconFolder, IconBell } from "../ui/icons";
import { ContextMenu, type MenuItem } from "../ui/ContextMenu";
import { RenameInput } from "../ui/RenameInput";
import { Checkbox } from "../Settings/shared";
import { ConfirmDialog } from "../Messages/ConfirmDialog";

// Re-export of the shared bucketed relative-date formatter so SearchOverlay's
// existing `formatRelativeDate` import keeps working without churn.
export const formatRelativeDate = formatRelativeBucket;

// NOTE: the one-time legacy → viewed-at migration that used to live here as a
// module-load IIFE now runs from main.tsx via `migrateReadSessions()` before
// React mounts. Keeping it out of this module avoids side effects under HMR
// and unit tests.

function isBellWorthy(session: { last_message_role: string | null; updated_at: string | null }): boolean {
  return session.last_message_role === "assistant" && !!session.updated_at;
}

/** Pure helper: compares an updated_at ISO string to a last-viewed ISO string. */
function hasUnreadUpdateFor(updatedAt: string | null, lastViewed: string | null): boolean {
  if (!updatedAt) return false;
  if (!lastViewed) return true; // never viewed
  return updatedAt > lastViewed; // ISO comparison is lexicographic-safe
}

// ---------------------------------------------------------------------------
// SessionItem
// ---------------------------------------------------------------------------

interface SessionItemProps {
  session: Session;
  isActive: boolean;
  onClick: () => void;
  /**
   * Live runner status for this session — populated by the Sidebar's shared
   * polling hook (issue #12). Undefined means "not yet polled" or "no Headless
   * runner managed by agent-decoder" (e.g. session driven by external CLI).
   * Kept as a prop so SessionItem stays presentational and unit-testable.
   */
  runnerStatus?: RunnerStatus;
  /**
   * Selection-mode click handler with shift-range support. Only the caller
   * knows the current visible session order (flat sort vs. project groups),
   * so range computation lives there — this just reports id + shiftKey.
   * Omitted callers (e.g. the starred section) fall back to a plain toggle.
   */
  onToggleSelect?: (id: string, shiftKey: boolean) => void;
}

export function SessionItem({ session, isActive, onClick, runnerStatus, onToggleSelect }: SessionItemProps) {
  const [hovered, setHovered] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [menuAnchor, setMenuAnchor] = useState<DOMRect | null>(null);
  const [isRenaming, setIsRenaming] = useState(false);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const starred = useStarredSet();
  const archived = useArchivedSet();
  const viewed = useViewedAt();
  const selectionMode = useAppStore((s) => s.selectionMode);
  const showProviderTag = useAppStore((s) => s.showProviderTag);
  const selectedSessionIds = useAppStore((s) => s.selectedSessionIds);
  const toggleSessionSelected = useAppStore((s) => s.toggleSessionSelected);
  const isSelected = selectedSessionIds.has(session.id);
  const isStarred = starred.has(session.id);
  const isArchived = archived.has(session.id);
  // Issue #11: server-side custom_title is the source of truth; localStorage
  // is just a write-through cache for offline resilience + flash-of-stale
  // prevention before the first /api/sessions response.
  const [customTitle, setCustomTitle] = useState<string | null>(
    () => session.custom_title ?? lsGetMap(LS.RENAMED)[session.id] ?? null
  );

  // When the server-side override is present, reconcile to it and refresh
  // the cache. When null, we *don't* preemptively clear the cache — it may
  // be a legitimate cached rename made offline or before the API existed.
  // Explicit clears flow through the "rename" bus (see effect below) where
  // we trust the empty payload as authoritative.
  useEffect(() => {
    if (session.custom_title) {
      setCustomTitle(session.custom_title);
      const m = lsGetMap(LS.RENAMED);
      if (m[session.id] !== session.custom_title) {
        m[session.id] = session.custom_title;
        lsPutMap(LS.RENAMED, m);
      }
    }
  }, [session.id, session.custom_title]);

  // SSE-driven rename reconciliation: another client (or our own write)
  // emits "rename" on the bus → update in-memory state and the cache.
  useEffect(() => {
    return on("rename", ({ id, title }) => {
      if (id !== session.id) return;
      const normalised = title.trim() === "" ? null : title;
      setCustomTitle(normalised);
      const m = lsGetMap(LS.RENAMED);
      if (normalised === null) {
        delete m[session.id];
      } else {
        m[session.id] = normalised;
      }
      lsPutMap(LS.RENAMED, m);
    });
  }, [session.id]);

  const menuBtnRef = useRef<HTMLButtonElement>(null);
  const displayTitle = customTitle ?? session.title ?? "Untitled";
  // Host-side actions (Open in terminal, Reveal in Finder) only make sense
  // when the client is connecting from the same machine the server runs on.
  // For a remote viewer over the network, these would fire silently on the
  // SERVER's host and confuse the user. Treat unknown host info as local so
  // we don't disable the buttons during the boot fetch.
  const hostInfo = useAppStore((s) => s.hostInfo);
  const remoteClient = hostInfo?.is_remote_client === true;

  // Bell — shown when the assistant's last message is awaiting a human reply AND the
  // session has been updated since the user last viewed it. Read state is a map
  // of sessionId → lastViewedAt (ISO), so bells survive rescans.
  const [bellState, setBellState] = useState<"visible" | "fading" | "hidden">(() => {
    if (!isBellWorthy(session)) return "hidden";
    return hasUnreadUpdateFor(session.updated_at, viewed.get(session.id)) ? "visible" : "hidden";
  });

  useEffect(() => {
    if (!isBellWorthy(session)) {
      setBellState("hidden");
      return;
    }
    if (hasUnreadUpdateFor(session.updated_at, viewed.get(session.id))) {
      setBellState("visible");
    }
    // ``viewed.map`` identity changes on any cache mutation — re-evaluates
    // the bell when another tab marks this session viewed.
  }, [session.id, session.updated_at, session.last_message_role, viewed.map]);

  // After fade-out completes, remove from DOM. (LocalStorage was already
  // written in handleClick — we don't rely on this timeout for persistence.)
  useEffect(() => {
    if (bellState !== "fading") return;
    const t = setTimeout(() => setBellState("hidden"), UI.BELL_FADE_MS);
    return () => clearTimeout(t);
  }, [bellState]);

  // Dismiss bell when the browser window regains focus while this session is active.
  useEffect(() => {
    if (!isActive) return;
    const onFocus = () => {
      setBellState((prev) => {
        if (prev !== "visible") return prev;
        if (session.updated_at) viewed.set(session.id, session.updated_at);
        return "fading";
      });
    };
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [isActive, session.id, session.updated_at, viewed]);

  const startRename = useCallback(() => {
    setIsRenaming(true);
  }, []);

  const commitRename = useCallback((value: string) => {
    // Optimistic: paint the new title immediately. Write-through cache so a
    // reload before the request returns still shows the user's intent.
    const trimmed = value.trim();
    const optimistic = trimmed === "" ? null : trimmed;
    const m = lsGetMap(LS.RENAMED);
    if (optimistic === null) {
      delete m[session.id];
    } else {
      m[session.id] = optimistic;
    }
    lsPutMap(LS.RENAMED, m);
    setCustomTitle(optimistic);
    setIsRenaming(false);
    emit("rename", { id: session.id, title: optimistic ?? "" });
    // Server-authoritative reconcile. The SSE session-meta event from our
    // own write will round-trip through the bus, but we also reconcile here
    // in case the response disagrees (e.g. whitespace stripping).
    api
      .setSessionTitle(session.id, optimistic)
      .then((res) => {
        emit("rename", { id: session.id, title: res.custom_title ?? "" });
      })
      .catch(() => {
        // Keep the optimistic value — localStorage cache provides offline
        // resilience and the next reload will hit /api/sessions to re-sync.
      });
  }, [session.id]);

  const cancelRename = useCallback(() => {
    setIsRenaming(false);
  }, []);

  const handleConfirmDelete = useCallback(() => {
    setDeleteDialogOpen(false);
    // Optimistic: remove the row from the sidebar immediately. The backend
    // delete (messages + FTS) can take a few seconds for large sessions, and
    // there's no reason to make an already-confirmed deletion feel laggy.
    // "refresh" reconciles afterwards (and re-adds the row if it failed).
    emit("sessions-removed", [session.id]);
    api.deleteSessions([session.id])
      .then((res) => {
        if (res.failed.length > 0) {
          console.error("[delete] Some sessions failed to delete:", res.failed);
          emit("toast", { message: "Couldn't delete session — see console for details.", kind: "error" });
        }
        emit("refresh", undefined);
      })
      .catch((err: unknown) => {
        console.error("[delete] deleteSessions error:", err);
        emit("toast", { message: "Couldn't delete session — see console for details.", kind: "error" });
        emit("refresh", undefined); // restore the optimistically-removed row
      });
  }, [session.id]);

  const menuItems: MenuItem[] = [
    {
      label: isStarred ? "Unstar" : "Star",
      icon: <IconStar filled={isStarred} />,
      action: () => starred.toggle(session.id),
    },
    {
      label: "Rename",
      icon: <IconRename />,
      action: startRename,
    },
    {
      label: isArchived ? "Unarchive" : "Archive",
      icon: <IconArchive />,
      action: () => archived.toggle(session.id),
    },
    {
      label: "Delete",
      icon: (
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none"
          stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
          <polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6"/><path d="M14 11v6"/>
          <path d="M9 6V4h6v2"/>
        </svg>
      ),
      action: () => setDeleteDialogOpen(true),
      danger: true,
    },
    { kind: "separator" },
    {
      kind: "submenu",
      label: "More",
      items: [
        {
          label: "Copy title",
          icon: <IconCopy />,
          action: () => navigator.clipboard.writeText(displayTitle).catch(() => {}),
        },
        {
          label: "Copy project path",
          icon: <IconFolder />,
          action: () => navigator.clipboard.writeText(session.cwd ?? "").catch(() => {}),
        },
        {
          label: "Copy file path",
          icon: <IconCopy />,
          action: () => navigator.clipboard.writeText(session.file_path).catch(() => {}),
        },
        {
          label: "Copy session ID",
          icon: <IconCopy />,
          action: () => navigator.clipboard.writeText(session.id).catch(() => {}),
        },
        {
          label: "Copy resume command",
          icon: <IconTerminal />,
          action: () => {
            const parts = session.file_path.split("/");
            let bin = "claude";
            const idx = parts.indexOf("projects");
            if (idx > 0) {
              let j = idx - 1;
              while (j >= 0 && parts[j] === "config") j--;
              if (j >= 0) bin = parts[j].replace(/^\./, "");
            }
            let wt = "";
            if (session.is_worktree) {
              if (session.cwd) {
                const m = "/.claude/worktrees/";
                const wi = session.cwd.indexOf(m);
                if (wi >= 0) wt = session.cwd.slice(wi + m.length);
              }
              if (!wt && idx >= 0 && parts[idx + 1]) {
                const mangled = parts[idx + 1];
                const mk = "worktrees-";
                const wi = mangled.indexOf(mk);
                if (wi >= 0) wt = mangled.slice(wi + mk.length);
              }
            }
            const cmd = wt ? `${bin} -w ${wt} -r ${session.id}` : `${bin} -r ${session.id}`;
            navigator.clipboard.writeText(cmd).catch(() => {});
          },
        },
        { kind: "separator" as const },
        {
          label: remoteClient ? "Reveal in Finder (host-only)" : "Reveal in Finder",
          icon: <IconFolder />,
          action: remoteClient
            ? () => {}
            : () => api.revealSession(session.id).catch(() => {}),
          disabled: remoteClient,
        },
        {
          label: session.is_fork
            ? "Open in terminal (fork — not resumable)"
            : remoteClient
            ? "Open in terminal (host-only)"
            : "Open in terminal",
          icon: <IconTerminal />,
          action: session.is_fork || remoteClient
            ? () => {}
            : () => api.openTerminal(session.id).catch((e: unknown) => {
                emit("toast", {
                  message: e instanceof Error ? e.message : "Couldn't open a terminal",
                  kind: "error",
                });
              }),
          disabled: session.is_fork || remoteClient,
        },
      ],
    },
  ];

  const openMenu = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (menuBtnRef.current) {
      setMenuAnchor(menuBtnRef.current.getBoundingClientRect());
      setMenuOpen(true);
    }
  };

  const handleSelectToggle = (shiftKey: boolean) => {
    if (onToggleSelect) onToggleSelect(session.id, shiftKey);
    else toggleSessionSelected(session.id);
  };

  const handleClick = (e: React.MouseEvent) => {
    if (selectionMode) {
      handleSelectToggle(e.shiftKey);
      return;
    }
    if (bellState === "visible") {
      if (session.updated_at) viewed.set(session.id, session.updated_at);
      setBellState("fading");
    }
    onClick();
  };

  return (
    <>
      <div
        className={`hover-actions-parent sidebar-session-item${isActive && !selectionMode ? " sidebar-session-item--active" : ""}`}
        onMouseEnter={() => { setHovered(true); prefetch(session.id, api.getSession); }}
        onMouseLeave={() => setHovered(false)}
        style={{
          position: "relative",
          display: "flex",
          alignItems: "center",
          padding: 0,
          background: isActive && !selectionMode
            ? "var(--bg-sidebar-active)"
            : isSelected
            ? "var(--bg-sidebar-active)"
            : hovered
            ? "var(--bg-sidebar-hover)"
            : "transparent",
          borderRadius: "var(--radius-sm)",
          margin: "1px 6px",
          transition: "background var(--transition-fast)",
          minHeight: "30px",
          gap: "4px",
        }}
      >
        {isRenaming ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              flex: 1,
              padding: "5px 8px 5px 12px",
              gap: "4px",
              minWidth: 0,
            }}
          >
            <RenameInput
              initialValue={displayTitle}
              onCommit={commitRename}
              onCancel={cancelRename}
            />
          </div>
        ) : (
          <>
            {/* Selection checkbox — sibling of (never nested inside) the row
                button: Checkbox renders its own <button>, and a button inside
                a button is invalid HTML that double-fires the toggle. */}
            {selectionMode && (
              <span style={{ flexShrink: 0, display: "flex", paddingLeft: "12px" }}>
                <Checkbox
                  checked={isSelected}
                  onChange={(_, e) => handleSelectToggle(e.shiftKey)}
                />
              </span>
            )}
            <button
              type="button"
              onClick={handleClick}
              onMouseDown={(e) => { if (selectionMode && e.shiftKey) e.preventDefault(); }}
              aria-label={displayTitle}
              style={{
                display: "flex",
                alignItems: "center",
                flex: 1,
                minWidth: 0,
                padding: "5px 8px 5px 12px",
                gap: "4px",
                background: "none",
                border: "none",
                cursor: "pointer",
                font: "inherit",
                color: "inherit",
                textAlign: "left",
                userSelect: "none",
                outline: "none",
              }}
            >
              {/* Star indicator — hidden in selection mode to save space */}
              {!selectionMode && isStarred && (
                <span style={{ color: "var(--accent-orange)", flexShrink: 0, fontSize: "11px", display: "flex" }}>
                  <IconStar filled />
                </span>
              )}

              <span
                data-testid="session-title"
                title={displayTitle}
                style={{
                  flex: 1,
                  minWidth: 0,
                  fontSize: "14px",
                  color: "var(--text-primary)",
                  fontWeight: isActive ? 500 : 400,
                  lineHeight: "1.35",
                  whiteSpace: "nowrap",
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {displayTitle}
              </span>

              {/* Provider marker — opt-in (Settings → "Show provider tag on
                  chats", off by default) so rows aren't crowded as more CLIs
                  integrate. When on, kept deliberately subtle: a muted
                  monochrome label, no pill/border. */}
              {showProviderTag && !selectionMode && session.provider && session.provider !== "claude" && (
                <span
                  aria-label={`Provider: ${session.provider}`}
                  title={session.provider}
                  style={{
                    flexShrink: 0,
                    fontSize: "8.5px",
                    fontFamily: "var(--font-ui)",
                    fontWeight: 600,
                    letterSpacing: "0.06em",
                    color: "var(--text-tertiary)",
                    opacity: 0.7,
                    lineHeight: "14px",
                    userSelect: "none",
                  }}
                >
                  {session.provider === "codex" ? "GPT" : session.provider.toUpperCase()}
                </span>
              )}

              {/* Bell — right side, after title; hidden in selection mode */}
              {!selectionMode && bellState !== "hidden" && (
                <span
                  aria-label="Awaiting your reply"
                  style={{
                    flexShrink: 0,
                    display: "flex",
                    color: "var(--accent-orange)",
                    opacity:
                      bellState === "fading" || hovered || isActive || menuOpen
                        ? 0
                        : 1,
                    transition: "opacity var(--transition-fast)",
                  }}
                >
                  <IconBell />
                </span>
              )}

              {/* Busy marker — pulses while a PTY turn is in flight. */}
              {!selectionMode && runnerStatus?.busy && (
                <span
                  data-testid="runner-busy-marker"
                  aria-label="Running"
                  title="Running"
                  style={{
                    flexShrink: 0,
                    width: "8px",
                    height: "8px",
                    borderRadius: "50%",
                    background: "var(--accent-green)",
                    boxShadow: "0 0 0 0 var(--accent-green)",
                    animation: "clau-runner-pulse 1.6s ease-out infinite",
                    marginRight: "2px",
                  }}
                />
              )}
            </button>

            {/* ••• menu button — hidden in selection mode */}
            {!selectionMode && (
              <button
                ref={menuBtnRef}
                type="button"
                onClick={openMenu}
                className={isActive || menuOpen ? "" : "hover-actions"}
                style={{
                  position: "absolute",
                  right: "0",
                  top: 0,
                  bottom: 0,
                  // Match the row's right rounded corners — this button is flush
                  // against the row's right edge and paints the active/hover
                  // background, so without matching radii its square right edge
                  // masks the row's rounded corners on a selected/hovered item.
                  borderRadius: "0 var(--radius-sm) var(--radius-sm) 0",
                  color: menuOpen ? "var(--text-primary)" : "var(--text-tertiary)",
                  fontSize: "14px",
                  lineHeight: "30px",
                  padding: "0 8px",
                  letterSpacing: "1px",
                  background: isActive
                    ? "var(--bg-sidebar-active)"
                    : hovered || menuOpen
                    ? "var(--bg-sidebar-hover)"
                    : "var(--bg-sidebar)",
                  border: "none",
                  cursor: "pointer",
                  outline: "none",
                  fontFamily: "var(--font-ui)",
                  transition: "background var(--transition-fast)",
                }}
                aria-label="Session options"
              >
                •••
              </button>
            )}
          </>
        )}
      </div>

      {menuOpen && menuAnchor && (
        <ContextMenu
          items={menuItems}
          anchorRect={menuAnchor}
          onClose={() => setMenuOpen(false)}
        />
      )}

      {deleteDialogOpen && (
        <ConfirmDialog
          title="Delete session?"
          body="This permanently deletes the session transcript file from disk and cannot be undone."
          confirmLabel="Delete"
          onConfirm={handleConfirmDelete}
          onCancel={() => setDeleteDialogOpen(false)}
        />
      )}
    </>
  );
}
