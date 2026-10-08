import { useCallback, useState, useEffect, useMemo, useRef } from "react";
import React from "react";
import { OverlayScrollbarsComponent } from "overlayscrollbars-react";
import type { Project, Session } from "../../api/types";
import { api } from "../../api/client";
import { useAppStore } from "../../store";
import { navigateTo } from "../../router";
import { LS, lsGetRaw, lsSetRaw } from "../../utils/localStorage";
import { useArchivedSet, useStarredSet } from "../../utils/sessionMeta";
import { on, emit } from "../../utils/events";
import { SIDEBAR } from "../../config/ui";
import { SCROLLBAR_OPTIONS } from "../ScrollContainer";
import { SidebarHeader } from "./SidebarHeader";
import { ProjectGroup } from "./ProjectGroup";
import { SessionItem } from "./SessionItem";
import { FileExplorer } from "./FileExplorer";
import { useRunnerStatuses } from "./hooks/useRunnerStatuses";
import { ConfirmDialog } from "../Messages/ConfirmDialog";

function loadStoredSidebarWidth(): number {
  if (typeof window === "undefined") return SIDEBAR.DEFAULT_WIDTH;
  const raw = lsGetRaw(LS.SIDEBAR_WIDTH);
  const n = raw ? Number(raw) : NaN;
  if (!Number.isFinite(n)) return SIDEBAR.DEFAULT_WIDTH;
  return Math.max(SIDEBAR.MIN_WIDTH, n);
}

function IconSearch() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/>
    </svg>
  );
}

function IconChats() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>
  );
}

function IconSettings() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="3"/>
      <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
    </svg>
  );
}

function IconHelp() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/>
    </svg>
  );
}

function IconKeyboard() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="2" y="4" width="20" height="16" rx="2"/><line x1="6" y1="8" x2="6.01" y2="8"/><line x1="10" y1="8" x2="10.01" y2="8"/><line x1="14" y1="8" x2="14.01" y2="8"/><line x1="18" y1="8" x2="18.01" y2="8"/><line x1="8" y1="12" x2="8.01" y2="12"/><line x1="12" y1="12" x2="12.01" y2="12"/><line x1="16" y1="12" x2="16.01" y2="12"/><line x1="7" y1="16" x2="17" y2="16"/>
    </svg>
  );
}

// Section header for the flat session list ("Recents" / "Archived").
// On hover it reveals a "Select" affordance on the right that enters
// multi-select mode — replacing the old dedicated nav button.
function RecentsHeader({
  label,
  onSelect,
  selectionMode,
}: {
  label: string;
  onSelect: () => void;
  selectionMode: boolean;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        display: "flex",
        alignItems: "center",
        width: "100%",
        padding: "6px 18px 4px",
        fontSize: "11px",
        fontWeight: 600,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: "var(--text-tertiary)",
        fontFamily: "var(--font-ui)",
        gap: "4px",
        minHeight: "22px",
      }}
    >
      <span style={{ flex: 1, textAlign: "left" }}>{label}</span>
      {!selectionMode && (
        <button
          type="button"
          onClick={onSelect}
          aria-label="Select sessions"
          style={{
            display: "flex",
            alignItems: "center",
            gap: "4px",
            background: "none",
            border: "none",
            cursor: "pointer",
            padding: "2px 4px",
            margin: "-2px -4px -2px 0",
            borderRadius: "var(--radius-sm)",
            color: hovered ? "var(--accent-orange)" : "var(--text-tertiary)",
            fontFamily: "var(--font-ui)",
            fontSize: "10px",
            fontWeight: 600,
            textTransform: "uppercase",
            letterSpacing: "0.06em",
            opacity: hovered ? 1 : 0,
            pointerEvents: hovered ? "auto" : "none",
            transition: "opacity var(--transition-fast), color var(--transition-fast)",
          }}
        >
          <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="3" y="5" width="4" height="4" rx="1"/>
            <line x1="10" y1="7" x2="21" y2="7"/>
            <rect x="3" y="13" width="4" height="4" rx="1"/>
            <line x1="10" y1="15" x2="21" y2="15"/>
          </svg>
          Select
        </button>
      )}
    </div>
  );
}

function BulkActionToolbar({
  selectedCount,
  onSelectAll,
  onArchive,
  onDelete,
  onCancel,
}: {
  selectedCount: number;
  onSelectAll: () => void;
  onArchive: () => void;
  onDelete: () => void;
  onCancel: () => void;
}) {
  const noneSelected = selectedCount === 0;
  return (
    <div style={{
      padding: "8px 10px",
      background: "var(--bg-sidebar-active)",
      borderBottom: "1px solid var(--border-subtle)",
      borderRadius: "var(--radius-sm)",
      margin: "0 6px 4px",
      display: "flex",
      flexDirection: "column",
      gap: "6px",
    }}>
      <div style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        fontSize: "12px",
        color: "var(--text-secondary)",
        fontFamily: "var(--font-ui)",
      }}>
        <span style={{ fontWeight: 500 }}>
          {selectedCount} selected
        </span>
        <button
          onClick={onSelectAll}
          style={{
            background: "none",
            border: "none",
            cursor: "pointer",
            fontSize: "12px",
            color: "var(--accent-orange)",
            fontFamily: "var(--font-ui)",
            padding: "0",
          }}
        >
          Select all
        </button>
      </div>
      <div style={{ display: "flex", gap: "6px" }}>
        <BulkActionBtn
          label="Archive"
          disabled={noneSelected}
          onClick={onArchive}
        />
        <BulkActionBtn
          label="Delete"
          disabled={noneSelected}
          danger
          onClick={onDelete}
        />
        <BulkActionBtn
          label="Cancel"
          disabled={false}
          onClick={onCancel}
        />
      </div>
    </div>
  );
}

function BulkActionBtn({
  label,
  disabled,
  danger,
  onClick,
}: {
  label: string;
  disabled: boolean;
  danger?: boolean;
  onClick: () => void;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{
        flex: 1,
        padding: "4px 6px",
        fontSize: "12px",
        fontFamily: "var(--font-ui)",
        background: disabled
          ? "none"
          : danger
          ? hovered ? "#b85c5c" : "#c47a7a"
          : hovered
          ? "var(--bg-sidebar-hover)"
          : "var(--bg-tool-block)",
        color: disabled
          ? "var(--text-tertiary)"
          : danger
          ? "#fff"
          : "var(--text-primary)",
        border: disabled ? "1px solid var(--border-subtle)" : "1px solid var(--border-default)",
        borderRadius: "var(--radius-sm)",
        cursor: disabled ? "default" : "pointer",
        transition: "background var(--transition-fast), color var(--transition-fast)",
        opacity: disabled ? 0.5 : 1,
      }}
    >
      {label}
    </button>
  );
}

function NavItem({
  icon,
  label,
  shortcut,
  active,
  onClick,
  collapsed,
  fade,
}: {
  icon: React.ReactNode;
  label: string;
  shortcut?: string;
  active?: boolean;
  onClick?: () => void;
  collapsed?: boolean;
  fade?: boolean;
}) {
  const [hovered, setHovered] = useState(false);
  return (
    <button
      onClick={onClick}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      title={collapsed ? label : undefined}
      style={{
        display: "flex",
        alignItems: "center",
        gap: "10px",
        width: "calc(100% - 12px)",
        padding: "7px 12px",
        background: active
          ? "var(--bg-sidebar-active)"
          : hovered
          ? "var(--bg-sidebar-hover)"
          : "none",
        border: "none",
        borderRadius: "var(--radius-sm)",
        cursor: "pointer",
        color: "var(--text-primary)",
        fontSize: "15px",
        fontFamily: "var(--font-ui)",
        fontWeight: active ? 500 : 400,
        textAlign: "left",
        transition: "background var(--transition-fast), color var(--transition-fast), opacity var(--transition-medium)",
        margin: "1px 6px",
        opacity: fade ? 0 : undefined,
        overflow: "hidden",
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ flexShrink: 0, display: "flex" }}>{icon}</span>
      <span style={{
        flex: 1,
        overflow: "hidden",
        whiteSpace: "nowrap",
        opacity: collapsed ? 0 : 1,
        transition: "opacity var(--transition-medium)",
      }}>{label}</span>
      {shortcut && (
        <kbd style={{
          fontSize: "11px",
          color: "var(--text-tertiary)",
          fontFamily: "var(--font-ui)",
          opacity: collapsed ? 0 : 1,
          transition: "opacity var(--transition-medium)",
        }}>
          {shortcut}
        </kbd>
      )}
    </button>
  );
}

function SidebarFooter({ collapsed }: { collapsed?: boolean }) {
  const openSettings = useAppStore((s) => s.openSettings);
  const openHelp = useAppStore((s) => s.openHelp);
  const openShortcuts = useAppStore((s) => s.openShortcuts);
  const profiles = useAppStore((s) => s.profiles);
  const activeProfileId = useAppStore((s) => s.activeProfileId);
  const setActiveProfileId = useAppStore((s) => s.setActiveProfileId);
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    const onClick = (e: MouseEvent) => {
      if (menuRef.current && !menuRef.current.contains(e.target as Node)) {
        setMenuOpen(false);
      }
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, [menuOpen]);

  useEffect(() => {
    if (!menuOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setMenuOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [menuOpen]);

  useEffect(() => {
    if (collapsed) setMenuOpen(false);
  }, [collapsed]);

  const switchProfile = async (id: string | null) => {
    try {
      await api.setActiveProfile(id);
      setActiveProfileId(id);
      setMenuOpen(false);
    } catch { /* ignore */ }
  };

  const activeProfile = profiles.find((p) => p.id === activeProfileId);
  const displayName = activeProfile ? activeProfile.name : profiles.length > 0 ? "All Profiles" : "Agent Decoder";
  const displayColor = activeProfile ? activeProfile.color : "var(--accent-orange)";
  const initial = activeProfile ? (activeProfile.name?.[0] ?? "?").toUpperCase() : "C";

  const menuItemStyle = (highlight?: boolean): React.CSSProperties => ({
    display: "flex",
    alignItems: "center",
    gap: "10px",
    width: "100%",
    padding: "7px 12px",
    background: highlight ? "var(--bg-sidebar-hover)" : "none",
    border: "none",
    cursor: "pointer",
    fontFamily: "var(--font-ui)",
    fontSize: "15px",
    textAlign: "left",
    color: "var(--text-primary)",
    transition: "background var(--transition-fast)",
  });

  const avatar = (
    <span
      style={{
        width: "28px",
        height: "28px",
        borderRadius: "50%",
        background: displayColor,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        flexShrink: 0,
        color: "var(--text-on-accent)",
        fontSize: "12px",
        fontWeight: 700,
      }}
    >
      {initial}
    </span>
  );

  return (
    <div style={{ borderTop: "1px solid var(--border-subtle)", padding: "8px 4px", flexShrink: 0, position: "relative" }} ref={menuRef}>
      <button
        onClick={collapsed ? () => { openSettings(); } : () => setMenuOpen(!menuOpen)}
        aria-label="Open menu"
        title={collapsed ? "Settings" : undefined}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "10px",
          width: "100%",
          padding: "8px 8px",
          background: "none",
          border: "none",
          borderRadius: "var(--radius-sm)",
          cursor: "pointer",
          outline: "none",
          fontFamily: "var(--font-ui)",
          overflow: "hidden",
        }}
      >
        {avatar}
        <div style={{
          flex: 1,
          textAlign: "left",
          overflow: "hidden",
          opacity: collapsed ? 0 : 1,
          transition: "opacity var(--transition-medium)",
          whiteSpace: "nowrap",
        }}>
          <div style={{ fontSize: "13px", fontWeight: 500, color: "var(--text-primary)" }}>{displayName}</div>
          <div style={{ fontSize: "11px", color: "var(--text-tertiary)" }}>Menu</div>
        </div>
      </button>

      {menuOpen && (
        <div style={{
          position: "absolute",
          bottom: "100%",
          left: "4px",
          right: "4px",
          background: "var(--bg-modal)",
          borderRadius: "12px",
          boxShadow: "0 -2px 12px rgba(0,0,0,0.08)",
          marginBottom: "-1px",
          overflow: "hidden",
        }}>
          {/* Settings */}
          <button
            onClick={() => { openSettings(); setMenuOpen(false); }}
            style={menuItemStyle()}
            onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = "none"; }}
          >
            <span style={{ flexShrink: 0, display: "flex", color: "var(--text-tertiary)" }}><IconSettings /></span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>Settings</div>
            </div>
          </button>


          {/* Get Help */}
          <button
            onClick={() => { openHelp(); setMenuOpen(false); }}
            style={menuItemStyle()}
            onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = "none"; }}
          >
            <span style={{ flexShrink: 0, display: "flex", color: "var(--text-tertiary)" }}><IconHelp /></span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>Get Help</div>
            </div>
          </button>

          {/* Keyboard Shortcuts */}
          <button
            onClick={() => { openShortcuts(); setMenuOpen(false); }}
            style={menuItemStyle()}
            onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.background = "none"; }}
          >
            <span style={{ flexShrink: 0, display: "flex", color: "var(--text-tertiary)" }}><IconKeyboard /></span>
            <div style={{ flex: 1 }}>
              <div style={{ fontWeight: 500 }}>Keyboard Shortcuts</div>
            </div>
          </button>

          {/* Profile section */}
          {profiles.length > 0 && (
            <>
              <div style={{ borderTop: "1px solid var(--border-subtle)", margin: "4px 0" }} />
              {/* All Profiles */}
              <button
                onClick={() => { switchProfile(null); }}
                style={menuItemStyle(!activeProfileId)}
              >
                <span style={{ width: 22, height: 22, borderRadius: "50%", background: "var(--accent-orange)", display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-on-accent)", fontSize: "10px", fontWeight: 700, flexShrink: 0 }}>C</span>
                <span style={{ fontWeight: !activeProfileId ? 500 : 400 }}>All Profiles</span>
              </button>
              {profiles.map((p) => (
                <button
                  key={p.id}
                  onClick={() => { switchProfile(p.id); }}
                  style={menuItemStyle(activeProfileId === p.id)}
                >
                  <span style={{ width: 22, height: 22, borderRadius: "50%", background: p.color, display: "flex", alignItems: "center", justifyContent: "center", color: "var(--text-on-accent)", fontSize: "10px", fontWeight: 700, flexShrink: 0 }}>
                    {(p.name?.[0] ?? "?").toUpperCase()}
                  </span>
                  <span style={{ fontWeight: activeProfileId === p.id ? 500 : 400 }}>{p.name}</span>
                </button>
              ))}
            </>
          )}
        </div>
      )}
    </div>
  );
}

export default function Sidebar() {
  const [projects, setProjects] = useState<Project[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [expandedProjects, setExpandedProjects] = useState<Set<string>>(new Set());

  // Flat sessions for date-based sort
  const [flatSessions, setFlatSessions] = useState<Session[]>([]);
  const [flatLoading, setFlatLoading] = useState(false);
  // True once we've completed the initial session load — refreshes should not show a spinner
  const flatLoadedRef = useRef(false);

  const sessionListRef = useRef<React.ComponentRef<typeof OverlayScrollbarsComponent>>(null);

  // Archive view
  const [showArchive, setShowArchive] = useState(false);
  const archived = useArchivedSet();
  const starred = useStarredSet();

  const openSearch = useAppStore((s) => s.openSearch);
  const sessionSortOrder = useAppStore((s) => s.sessionSortOrder);
  const selectedSessionId = useAppStore((s) => s.selectedSessionId);
  const selectProject = useAppStore((s) => s.selectProject);
  const sidebarCollapsed = useAppStore((s) => s.sidebarCollapsed);
  const sidebarMode = useAppStore((s) => s.sidebarMode);
  const setFileExplorerRoot = useAppStore((s) => s.setFileExplorerRoot);
  const activeProfileId = useAppStore((s) => s.activeProfileId);
  const setProfiles = useAppStore((s) => s.setProfiles);
  const setActiveProfileId = useAppStore((s) => s.setActiveProfileId);

  // Multi-select
  const selectionMode = useAppStore((s) => s.selectionMode);
  const selectedSessionIds = useAppStore((s) => s.selectedSessionIds);
  const selectionAnchorId = useAppStore((s) => s.selectionAnchorId);
  const setSelectionAnchorId = useAppStore((s) => s.setSelectionAnchorId);
  const toggleSessionSelected = useAppStore((s) => s.toggleSessionSelected);
  const enterSelectionMode = useAppStore((s) => s.enterSelectionMode);
  const exitSelectionMode = useAppStore((s) => s.exitSelectionMode);
  const clearSelection = useAppStore((s) => s.clearSelection);
  const setSelectedSessionIds = useAppStore((s) => s.setSelectedSessionIds);

  // Bulk-delete confirm dialog
  const [bulkDeleteDialogOpen, setBulkDeleteDialogOpen] = useState(false);

  const showParentFolder = useAppStore((s) => s.showParentFolder);

  const showFlat = sessionSortOrder !== "alpha";

  // Sidebar width — drag-resizable from the right edge, persisted across sessions.
  // Collapsed state always overrides to 52px; this state only governs expanded width.
  const [sidebarWidth, setSidebarWidth] = useState<number>(loadStoredSidebarWidth);
  const sidebarWidthRef = useRef(sidebarWidth);
  sidebarWidthRef.current = sidebarWidth;
  const [resizingSidebar, setResizingSidebar] = useState(false);

  // When dragging narrow, treat components as collapsed so text fades but icons stay.
  // Uses a ref so once fading starts during a drag it stays faded (prevents flashing
  // when dragging near the threshold).
  const fadeCollapsedRef = useRef(false);
  if (resizingSidebar && sidebarWidth < SIDEBAR.FADE_TEXT_MIN_PX) fadeCollapsedRef.current = true;
  if (resizingSidebar && sidebarWidth > SIDEBAR.FADE_TEXT_MAX_PX) fadeCollapsedRef.current = false;
  if (!resizingSidebar) fadeCollapsedRef.current = false;
  const fadeCollapsed = fadeCollapsedRef.current;
  const textCollapsed = sidebarCollapsed || fadeCollapsed;

  // Clamp stored width into a sane range for the current viewport. Runs on
  // mount and on window resize so dragging the browser narrow can't strand
  // the sidebar wider than the available space.
  useEffect(() => {
    const clamp = () => {
      const vw = window.innerWidth;
      // Auto-collapse when the window is too narrow for both sidebar + main pane.
      if (vw < SIDEBAR.COLLAPSED_WIDTH + SIDEBAR.MIN_MAIN_PANE) {
        useAppStore.getState().setSidebarCollapsed(true);
        return;
      }
      // Only viewport-derived cap: leave room for the main pane.
      const maxByViewport = Math.max(SIDEBAR.MIN_WIDTH, vw - SIDEBAR.MIN_MAIN_PANE);
      setSidebarWidth((w) => Math.max(SIDEBAR.MIN_WIDTH, Math.min(w, maxByViewport)));
    };
    clamp();
    window.addEventListener("resize", clamp);
    return () => window.removeEventListener("resize", clamp);
  }, []);

  // Persist the user's preferred expanded width whenever it changes.
  // Skips transient drag values and collapsed-width snaps so localStorage
  // always reflects the last fully-committed user width.
  useEffect(() => {
    if (resizingSidebar) return;
    if (sidebarWidth < SIDEBAR.SNAP_THRESHOLD) return;
    lsSetRaw(LS.SIDEBAR_WIDTH, String(sidebarWidth));
  }, [sidebarWidth, resizingSidebar]);

  const startSidebarResize = useCallback((e: React.MouseEvent) => {
    e.preventDefault();
    const startX = e.clientX;
    const wasCollapsed = sidebarCollapsed;
    const startWidth = wasCollapsed ? SIDEBAR.COLLAPSED_WIDTH : sidebarWidthRef.current;
    const storedExpanded = loadStoredSidebarWidth();
    setResizingSidebar(true);
    if (wasCollapsed) {
      setSidebarWidth(SIDEBAR.COLLAPSED_WIDTH);
    }
    document.body.style.userSelect = "none";
    const onMove = (ev: MouseEvent) => {
      const dx = ev.clientX - startX;
      const maxByViewport = Math.max(SIDEBAR.MIN_WIDTH, window.innerWidth - SIDEBAR.MIN_MAIN_PANE);
      const anchor = wasCollapsed ? SIDEBAR.COLLAPSED_WIDTH : startWidth;
      const raw = Math.max(SIDEBAR.COLLAPSED_WIDTH, Math.min(anchor + dx, maxByViewport));
      setSidebarWidth(raw);
      // Uncollapse only once the user drags past the snap threshold
      if (wasCollapsed && raw >= SIDEBAR.SNAP_THRESHOLD) {
        useAppStore.getState().setSidebarCollapsed(false);
      }
    };
    const onUp = () => {
      const final = sidebarWidthRef.current;
      if (final < SIDEBAR.SNAP_THRESHOLD) {
        useAppStore.getState().setSidebarCollapsed(true);
        setResizingSidebar(false);
        setSidebarWidth(storedExpanded);
      } else {
        useAppStore.getState().setSidebarCollapsed(false);
        setSidebarWidth(final);
        setResizingSidebar(false);
      }
      document.body.style.userSelect = "";
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
    };
    document.addEventListener("mousemove", onMove);
    document.addEventListener("mouseup", onUp);
  }, [sidebarCollapsed]);

  // Bulk archive: add all selected ids via the server-backed handle.
  // Each .add() fires a PUT and updates the shared cache; the SSE echo
  // syncs other tabs/browsers automatically.
  const handleBulkArchive = useCallback(() => {
    for (const id of selectedSessionIds) {
      archived.add(id);
    }
    clearSelection();
    exitSelectionMode();
  }, [selectedSessionIds, archived, clearSelection, exitSelectionMode]);

  const handleBulkDelete = useCallback(() => {
    setBulkDeleteDialogOpen(true);
  }, []);

  const handleConfirmBulkDelete = useCallback(() => {
    const ids = [...selectedSessionIds];
    setBulkDeleteDialogOpen(false);
    clearSelection();
    exitSelectionMode();
    // Optimistic: drop the rows from every list immediately (the backend delete
    // of messages + FTS can take a few seconds for large sessions). "refresh"
    // reconciles once the request resolves, re-adding any that actually failed.
    emit("sessions-removed", ids);
    api.deleteSessions(ids)
      .then((res) => {
        if (res.failed.length > 0) {
          console.error("[bulk-delete] Some sessions failed to delete:", res.failed);
          emit("toast", {
            message: `Couldn't delete ${res.failed.length} session${res.failed.length === 1 ? "" : "s"}.`,
            kind: "error",
          });
        }
        emit("refresh", undefined);
      })
      .catch((err: unknown) => {
        console.error("[bulk-delete] deleteSessions error:", err);
        emit("toast", { message: "Couldn't delete sessions — see console for details.", kind: "error" });
        emit("refresh", undefined); // restore optimistically-removed rows
      });
  }, [selectedSessionIds, clearSelection, exitSelectionMode]);

  const handleSelectSession = (session: Session) => {
    selectProject(session.project_id);
    setFileExplorerRoot(session.cwd);
    navigateTo(`/chat/${session.id}`);
  };

  // When switching to folder mode with a session already selected, set the root.
  useEffect(() => {
    if (sidebarMode !== "folder") return;
    const root = useAppStore.getState().fileExplorerRoot;
    if (root) return;
    const sid = selectedSessionId;
    if (!sid) return;
    const session = flatSessions.find((s) => s.id === sid);
    if (session?.cwd) setFileExplorerRoot(session.cwd);
  }, [sidebarMode]);

  const displayName = (project: Project) => {
    if (showParentFolder) return project.display_name;
    const idx = project.display_name.lastIndexOf("/");
    return idx >= 0 ? project.display_name.slice(idx + 1) : project.display_name;
  };

  const sortedProjects = useMemo(() => {
    if (sessionSortOrder !== "alpha") return projects;
    return [...projects].sort((a, b) =>
      displayName(a).localeCompare(displayName(b))
    );
  }, [projects, sessionSortOrder, showParentFolder]);

  // Load profiles on mount
  useEffect(() => {
    api.getProfiles().then((data) => {
      setProfiles(data.profiles);
      setActiveProfileId(data.active_profile_id);
    }).catch(() => {});
  }, []);

  // Re-fetch when active profile changes
  useEffect(() => {
    if (activeProfileId === undefined) return; // skip initial undefined
    let cancelled = false;
    setLoading(true);
    api.getProjects().then((data) => {
      if (!cancelled) {
        setProjects(data);
        if (data.length > 0) setExpandedProjects(new Set([data[0].id]));
      }
    }).catch(() => {}).finally(() => { if (!cancelled) setLoading(false); });
    api.getAllSessions().then((s) => { if (!cancelled) setFlatSessions(s); }).catch(() => {});
    return () => { cancelled = true; };
  }, [activeProfileId]);

  // Load projects
  useEffect(() => {
    let cancelled = false;
    api
      .getProjects()
      .then((data) => {
        if (!cancelled) {
          setProjects(data);
          if (data.length > 0) setExpandedProjects(new Set([data[0].id]));
        }
      })
      .catch((err: unknown) => {
        if (!cancelled)
          setError(err instanceof Error ? err.message : "Failed to load projects");
      })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, []);

  // Reload all sessions on project list change. Single flat request instead of N per-project.
  useEffect(() => {
    if (projects.length === 0) return;
    let cancelled = false;
    if (!flatLoadedRef.current) setFlatLoading(true);
    api.getAllSessions()
      .then((sessions) => {
        if (!cancelled) {
          flatLoadedRef.current = true;
          setFlatSessions(sessions);
        }
      })
      .catch(() => {})
      .finally(() => { if (!cancelled) setFlatLoading(false); });
    return () => { cancelled = true; };
  }, [projects]);

  // SSE refresh — update both projects and sessions directly without showing spinners
  useEffect(() => {
    return on("refresh", () => {
      api.getProjects().then(setProjects).catch(() => {});
      api.getAllSessions().then(setFlatSessions).catch(() => {});
    });
  }, []);

  // Optimistic delete — drop rows from the flat list the instant the user
  // confirms, before the (potentially slow) backend delete resolves.
  useEffect(() => {
    return on("sessions-removed", (ids) => {
      const gone = new Set(ids);
      setFlatSessions((prev) => prev.filter((s) => !gone.has(s.id)));
    });
  }, []);

  const starredSessions = useMemo(() =>
    flatSessions.filter((s) => starred.has(s.id) && !archived.has(s.id)),
    [flatSessions, starred, archived]
  );

  const sortedFlatSessions = useMemo(() => {
    const filtered = flatSessions.filter((s) =>
      showArchive ? archived.has(s.id) : !archived.has(s.id)
    );
    if (sessionSortOrder === "recent") {
      filtered.sort((a, b) =>
        (b.updated_at ?? b.started_at ?? "").localeCompare(a.updated_at ?? a.started_at ?? "")
      );
    } else if (sessionSortOrder === "oldest") {
      filtered.sort((a, b) =>
        (a.updated_at ?? a.started_at ?? "").localeCompare(b.updated_at ?? b.started_at ?? "")
      );
    }
    return filtered;
  }, [flatSessions, sessionSortOrder, showArchive, archived]);

  // Issue #12 — poll busy snapshots for sessions visible in the flat list
  // (recents / archive) and the starred section. Project groups poll their
  // own sessions separately so we don't have to predict expansion here.
  // Two coalesced timers worst case; far better than per-item polling.
  const visibleFlatIds = useMemo(() => {
    const ids = new Set<string>();
    for (const s of sortedFlatSessions) ids.add(s.id);
    for (const s of starredSessions) ids.add(s.id);
    return [...ids];
  }, [sortedFlatSessions, starredSessions]);
  const runnerStatuses = useRunnerStatuses(visibleFlatIds);

  const handleSelectAll = useCallback(() => {
    // Select all sessions currently visible (flat list or archive view)
    const visibleIds = sortedFlatSessions.map((s) => s.id);
    setSelectedSessionIds(visibleIds);
  }, [sortedFlatSessions, setSelectedSessionIds]);

  // Shift-click range select for the flat/archive list: a plain click moves
  // the anchor; a shift-click selects everything between the anchor and the
  // clicked row (inclusive), added to whatever's already selected.
  const handleToggleSessionSelected = useCallback(
    (id: string, shiftKey: boolean) => {
      if (shiftKey && selectionAnchorId) {
        const ids = sortedFlatSessions.map((s) => s.id);
        const anchorIdx = ids.indexOf(selectionAnchorId);
        const targetIdx = ids.indexOf(id);
        if (anchorIdx !== -1 && targetIdx !== -1) {
          const [start, end] = anchorIdx < targetIdx ? [anchorIdx, targetIdx] : [targetIdx, anchorIdx];
          const rangeIds = ids.slice(start, end + 1);
          setSelectedSessionIds([...new Set([...selectedSessionIds, ...rangeIds])]);
          return;
        }
      }
      toggleSessionSelected(id);
      setSelectionAnchorId(id);
    },
    [selectionAnchorId, sortedFlatSessions, selectedSessionIds, setSelectedSessionIds, toggleSessionSelected, setSelectionAnchorId]
  );

  const toggleProject = (projectId: string) => {
    setExpandedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(projectId)) next.delete(projectId);
      else next.add(projectId);
      return next;
    });
  };

  return (
    <aside
      aria-label="Navigation"
      style={{
        width: sidebarCollapsed && !resizingSidebar ? `${SIDEBAR.COLLAPSED_WIDTH}px` : `${Math.max(SIDEBAR.COLLAPSED_WIDTH, sidebarWidth)}px`,
        height: "100vh",
        display: "flex",
        flexDirection: "column",
        background: "var(--bg-sidebar)",
        borderRight: "1px solid var(--border-subtle)",
        flexShrink: 0,
        overflow: "hidden",
        position: "relative",
        transition: resizingSidebar ? "none" : "width var(--transition-medium)",
      }}
    >
      {/* Drag handle — always visible so you can expand from collapsed. */}
      <div
        onMouseDown={startSidebarResize}
        title="Drag to resize"
        aria-label="Resize sidebar"
        role="separator"
        aria-orientation="vertical"
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          right: 0,
          width: "4px",
          cursor: "col-resize",
          zIndex: 10,
          background: resizingSidebar ? "var(--border-default)" : "transparent",
          transition: "background 0.12s",
        }}
        onMouseEnter={(e) => {
          if (!resizingSidebar) e.currentTarget.style.background = "var(--border-default)";
        }}
        onMouseLeave={(e) => {
          if (!resizingSidebar) e.currentTarget.style.background = "transparent";
        }}
      />
      <SidebarHeader collapsed={textCollapsed} />

      {/* Nav items */}
      <div style={{ padding: "6px 0 4px", flexShrink: 0 }}>
        <NavItem collapsed={textCollapsed} icon={<IconSearch />} label="Search" shortcut="⌘K" onClick={openSearch} />

        {/* Starred section — only shown when there are starred sessions */}
        {!sidebarCollapsed && starredSessions.length > 0 && (
          <div style={{ margin: "4px 0" }}>
            <div
              style={{
                padding: "4px 18px 2px",
                fontSize: "11px",
                fontWeight: 600,
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                color: "var(--text-tertiary)",
                fontFamily: "var(--font-ui)",
              }}
            >
              Starred
            </div>
            {starredSessions.map((session) => (
              <SessionItem
                key={session.id}
                session={session}
                isActive={selectedSessionId === session.id}
                onClick={() => handleSelectSession(session)}
                runnerStatus={runnerStatuses.get(session.id)}
              />
            ))}
          </div>
        )}

        <NavItem
          collapsed={textCollapsed}
          icon={<IconChats />}
          label="Archive"
          active={showArchive}
          onClick={() => setShowArchive((v) => !v)}
          fade={textCollapsed}
        />
      </div>

      {!sidebarCollapsed && <div style={{ marginTop: "2px" }} />}

      {/* Sessions list — hidden via display when collapsed to keep OS instance alive */}
      <OverlayScrollbarsComponent
        ref={sessionListRef}
        options={SCROLLBAR_OPTIONS}
        role={sidebarMode === "chat" ? "list" : undefined}
        aria-label={sidebarMode === "chat" ? "Sessions" : undefined}
        style={{
          flex: 1,
          padding: sidebarMode === "folder" ? "0" : "8px 0",
          display: sidebarCollapsed ? "none" : undefined,
          opacity: textCollapsed ? 0 : 1,
          transition: "opacity var(--transition-medium)",
        }}
      >
        {sidebarMode === "folder" ? (
          <FileExplorer />
        ) : (
            <>
              {/* Bulk-action toolbar — shown when selection mode is active */}
              {selectionMode && (
                <BulkActionToolbar
                  selectedCount={selectedSessionIds.size}
                  onSelectAll={handleSelectAll}
                  onArchive={handleBulkArchive}
                  onDelete={handleBulkDelete}
                  onCancel={exitSelectionMode}
                />
              )}

              {loading && (
                <div style={{ padding: "16px", fontSize: "13px", color: "var(--text-tertiary)", textAlign: "center" }}>
                  Loading…
                </div>
              )}
              {error && (
                <div style={{ padding: "16px", fontSize: "13px", color: "var(--tool-error-border)" }}>
                  {error}
                </div>
              )}

              {/* Flat sorted list (recent / oldest) — also used for archive view in any sort mode */}
              {!loading && !error && (showFlat || showArchive) && (
                <>
                  <RecentsHeader
                    label={showArchive ? "Archived" : "Recents"}
                    onSelect={enterSelectionMode}
                    selectionMode={selectionMode}
                  />
                  {flatLoading && (
                    <div style={{ padding: "8px 16px", fontSize: "12px", color: "var(--text-tertiary)" }}>
                      Loading…
                    </div>
                  )}
                  {!flatLoading && sortedFlatSessions.map((session) => (
                    <SessionItem
                      key={session.id}
                      session={session}
                      isActive={selectedSessionId === session.id}
                      onClick={() => handleSelectSession(session)}
                      runnerStatus={runnerStatuses.get(session.id)}
                      onToggleSelect={handleToggleSessionSelected}
                    />
                  ))}
                  {!flatLoading && sortedFlatSessions.length === 0 && (
                    <div style={{ padding: "24px 16px", fontSize: "13px", color: "var(--text-tertiary)", textAlign: "center", lineHeight: 1.6 }}>
                      {showArchive ? "No archived sessions." : "No sessions found."}
                    </div>
                  )}
                </>
              )}

              {/* Project groups (alpha sort) — hidden when archive view is active */}
              {!loading && !error && !showFlat && !showArchive && (
                <>
                  {sortedProjects.map((project) => (
                    <ProjectGroup
                      key={project.id}
                      project={project}
                      displayName={displayName(project)}
                      isExpanded={expandedProjects.has(project.id)}
                      onToggle={() => toggleProject(project.id)}
                      archivedIds={archived.ids}
                    />
                  ))}
                  {sortedProjects.length === 0 && (
                    <div style={{ padding: "24px 16px", fontSize: "13px", color: "var(--text-tertiary)", textAlign: "center", lineHeight: 1.6 }}>
                      No projects found.<br />Add a path in Settings.
                    </div>
                  )}
                </>
              )}
            </>
          )}
        </OverlayScrollbarsComponent>

      {sidebarCollapsed && <div style={{ flex: 1 }} />}

      <SidebarFooter collapsed={textCollapsed} />

      {bulkDeleteDialogOpen && (
        <ConfirmDialog
          title={`Delete ${selectedSessionIds.size} session${selectedSessionIds.size === 1 ? "" : "s"}?`}
          body="This permanently deletes the session transcript files from disk and cannot be undone."
          confirmLabel="Delete"
          onConfirm={handleConfirmBulkDelete}
          onCancel={() => setBulkDeleteDialogOpen(false)}
        />
      )}
    </aside>
  );
}
