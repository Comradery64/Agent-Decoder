import { useState, useRef, useEffect, useCallback } from "react";
import type { PtyOwnership, Session } from "../../api/types";
import { api } from "../../api/client";
import { useAppStore } from "../../store";
import { emit } from "../../utils/events";
import { IconTerminal } from "../ui/icons";

// Small status dot for the Phase-0 ownership badge. Inline-rendered
// next to the model label so it doesn't disturb existing header layout.
function OwnershipBadge({ ownership }: { ownership: PtyOwnership | null }) {
  if (!ownership) return null;
  // Phase-1: prefer the structured sidecar metadata when present —
  // it tells us owner_kind/hostname/ui_endpoint, not just a pid.
  const fo = ownership.foreign_owner;
  const terminalLabel = fo
    ? `Open in ${fo.kind} @ ${fo.hostname} (pid ${fo.pid})`
        + (fo.ui_endpoint ? ` — ${fo.ui_endpoint}` : "")
    : ownership.foreign_pids.length > 0
      ? `Open in another terminal (pid ${ownership.foreign_pids.join(", ")})`
      : "Open in another terminal";
  const palette = {
    ours: {
      color: "var(--accent-green, #6aaa64)",
      label: "Attached here (agent-decoder)",
    },
    terminal: {
      color: "var(--accent-amber, #c9b870)",
      label: terminalLabel,
    },
    idle: {
      color: "var(--text-tertiary)",
      label: "Idle — no claude attached",
    },
  } as const;
  const p = palette[ownership.status];
  return (
    <span
      title={p.label}
      aria-label={p.label}
      style={{
        display: "inline-block",
        width: "8px",
        height: "8px",
        borderRadius: "50%",
        background: p.color,
        flexShrink: 0,
      }}
    />
  );
}

function formatModelDisplay(model: string): string {
  const withoutPrefix = model.replace(/^claude-/i, "");
  const normalised = withoutPrefix.replace(/-(\d+)(?:-(\d+))?$/, (_m, major, minor) =>
    minor !== undefined ? ` ${major}.${minor}` : ` ${major}`
  );
  return normalised.replace(/^(\w)/, (c) => c.toUpperCase());
}

function IconDownload() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
      <polyline points="7 10 12 15 17 10"/>
      <line x1="12" y1="15" x2="12" y2="3"/>
    </svg>
  );
}

interface ConversationHeaderProps {
  session: Session | null;
  ownership: PtyOwnership | null;
  viewMode?: "decoded" | "native" | "sbs";
  onViewModeChange?: (mode: "decoded" | "native" | "sbs") => void;
  /** Whether the active provider can be driven live (Native/Split bridge).
   * False for read-only / non-drivable providers (e.g. Codex pre-4e). */
  canDriveLive?: boolean;
  nativeStateLabel?: string | null;
  /** True when the driven agent is blocked on a prompt the user must answer in
   * Native. Recolors the state chip amber and turns it into a third "switch to
   * Native" click target. (native-input-required-plan.md, Part B) */
  nativeNeedsAction?: boolean;
}

export function ConversationHeader({
  session,
  ownership,
  viewMode = "decoded",
  onViewModeChange,
  canDriveLive = true,
  nativeStateLabel = null,
  nativeNeedsAction = false,
}: ConversationHeaderProps) {
  const title = session === null ? "Loading…" : (session.title ?? "Untitled");
  const modelLabel = session?.model ? formatModelDisplay(session.model) : null;
  const [exportOpen, setExportOpen] = useState(false);
  const [exporting, setExporting] = useState(false);
  const dropdownRef = useRef<HTMLDivElement>(null);
  const remoteClient = useAppStore((s) => s.hostInfo?.is_remote_client === true);
  const terminalDisabled = !session || session.is_fork || remoteClient;
  const terminalTitle = session?.is_fork
    ? "Open in terminal (fork — not resumable)"
    : remoteClient
    ? "Open in terminal (host-only)"
    : "Open in terminal";

  const handleClickOutside = useCallback((e: MouseEvent) => {
    if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
      setExportOpen(false);
    }
  }, []);

  useEffect(() => {
    if (exportOpen) {
      document.addEventListener("mousedown", handleClickOutside);
      return () => document.removeEventListener("mousedown", handleClickOutside);
    }
  }, [exportOpen, handleClickOutside]);

  const handleExport = async (format: "json" | "md") => {
    if (!session) return;
    setExporting(true);
    setExportOpen(false);
    try {
      const { blob, filename } = await api.exportSession(session.id, format);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = filename;
      a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      console.error("Export failed:", err);
    } finally {
      setExporting(false);
    }
  };

  const btnBase: React.CSSProperties = {
    background: "none",
    border: "none",
    cursor: "pointer",
    color: "var(--text-tertiary)",
    padding: "6px",
    borderRadius: "var(--radius-sm)",
    display: "flex",
    transition: "color var(--transition-fast), background var(--transition-fast)",
  };

  return (
    <div
      style={{
        position: "relative",
        flexShrink: 0,
      }}
    >
      <div
        style={{
          height: "var(--header-height)",
          display: "flex",
          alignItems: "center",
          padding: "0 20px",
          background: "var(--bg-base)",
          gap: "12px",
        }}
      >
        {/* Title */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "6px",
            flex: 1,
            minWidth: 0,
          }}
        >
          <h1
            style={{
              margin: 0,
              fontSize: "14px",
              fontWeight: 500,
              color: "var(--text-primary)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {title}
          </h1>
        </div>

        {/* Right side */}
        <div style={{ display: "flex", alignItems: "center", gap: "8px", flexShrink: 0 }}>
          {/* The Decoded/Native/Split toggle is meaningful only when the
              provider can be driven live — Native/Split ARE the live bridge.
              For a read-only provider we hide the whole group (Decoded is the
              only mode, so a single-button toggle would be noise). */}
          {canDriveLive && (
          <div
            role="group"
            aria-label="Conversation view"
            style={{
              display: "flex",
              alignItems: "center",
              padding: "2px",
              border: "1px solid var(--border-subtle)",
              borderRadius: "var(--radius-sm)",
              background: "var(--bg-subtle)",
            }}
          >
            {(["decoded", "native", "sbs"] as const).map((mode) => {
              const active = viewMode === mode;
              return (
                <button
                  key={mode}
                  type="button"
                  aria-pressed={active}
                  title={`${mode === "decoded" ? "Decoded" : mode === "native" ? "Native" : "Split"} view · ⇧⌘\\ cycles`}
                  onClick={() => onViewModeChange?.(mode)}
                  style={{
                    border: "none",
                    borderRadius: "calc(var(--radius-sm) - 2px)",
                    background: active ? "var(--bg-base)" : "transparent",
                    color: active ? "var(--text-primary)" : "var(--text-secondary)",
                    boxShadow: active ? "0 1px 2px rgba(0, 0, 0, 0.08)" : "none",
                    cursor: "pointer",
                    fontSize: "12px",
                    lineHeight: 1,
                    padding: "5px 8px",
                    transition: "background var(--transition-fast), color var(--transition-fast)",
                  }}
                >
                  {mode === "decoded" ? "Decoded" : mode === "native" ? "Native" : "Split"}
                </button>
              );
            })}
          </div>
          )}
          <OwnershipBadge ownership={ownership} />
          {nativeStateLabel && (
            // When the agent is blocked on a Native prompt, the chip becomes a
            // third "switch to Native" click target (alongside the banner and
            // the view toggle) and recolors amber so it can't be missed.
            nativeNeedsAction ? (
              <button
                type="button"
                onClick={() => onViewModeChange?.("native")}
                title="Native input required — switch to Native"
                style={{
                  fontSize: "12px",
                  fontWeight: 600,
                  color: "var(--text-primary)",
                  padding: "4px 7px",
                  border: "1px solid var(--accent-amber, #c9b870)",
                  borderRadius: "var(--radius-sm)",
                  background: "rgba(201, 184, 112, 0.12)",
                  cursor: "pointer",
                  fontFamily: "var(--font-ui)",
                }}
              >
                {nativeStateLabel}
              </button>
            ) : (
              <span
                style={{
                  fontSize: "12px",
                  color: "var(--text-secondary)",
                  padding: "4px 7px",
                  border: "1px solid var(--border-subtle)",
                  borderRadius: "var(--radius-sm)",
                  background: "var(--bg-subtle)",
                }}
              >
                {nativeStateLabel}
              </span>
            )
          )}
          {modelLabel && (
            <span
              style={{
                fontSize: "12px",
                color: "var(--text-secondary)",
              }}
            >
              {modelLabel}
            </span>
          )}

          {/* Export dropdown */}
          {session && (
            <div ref={dropdownRef} style={{ position: "relative" }}>
              <button
                onClick={() => setExportOpen(!exportOpen)}
                disabled={exporting}
                title="Export session"
                aria-label="Export session"
                style={btnBase}
                onMouseEnter={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.color = "var(--text-primary)";
                  (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)";
                }}
                onMouseLeave={(e) => {
                  (e.currentTarget as HTMLButtonElement).style.color = "var(--text-tertiary)";
                  (e.currentTarget as HTMLButtonElement).style.background = "none";
                }}
              >
                <IconDownload />
              </button>
              {exportOpen && (
                <div
                  style={{
                    position: "absolute",
                    right: 0,
                    top: "100%",
                    zIndex: 100,
                    background: "var(--bg-elevated)",
                    border: "1px solid var(--border-default)",
                    borderRadius: "var(--radius-md)",
                    boxShadow: "var(--shadow-lg)",
                    minWidth: "140px",
                    overflow: "hidden",
                  }}
                >
                  <button
                    onClick={() => handleExport("json")}
                    style={{
                      display: "block",
                      width: "100%",
                      padding: "8px 12px",
                      background: "none",
                      border: "none",
                      cursor: "pointer",
                      textAlign: "left",
                      fontSize: "13px",
                      color: "var(--text-primary)",
                    }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLButtonElement).style.background = "none";
                    }}
                  >
                    Export as JSON
                  </button>
                  <button
                    onClick={() => handleExport("md")}
                    style={{
                      display: "block",
                      width: "100%",
                      padding: "8px 12px",
                      background: "none",
                      border: "none",
                      cursor: "pointer",
                      textAlign: "left",
                      fontSize: "13px",
                      color: "var(--text-primary)",
                    }}
                    onMouseEnter={(e) => {
                      (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)";
                    }}
                    onMouseLeave={(e) => {
                      (e.currentTarget as HTMLButtonElement).style.background = "none";
                    }}
                  >
                    Export as Markdown
                  </button>
                </div>
              )}
            </div>
          )}

          <button
            onClick={() => {
              if (!session) return;
              api.openTerminal(session.id).catch((e: unknown) => {
                emit("toast", {
                  message: e instanceof Error ? e.message : "Couldn't open a terminal",
                  kind: "error",
                });
              });
            }}
            disabled={terminalDisabled}
            title={terminalTitle}
            aria-label={terminalTitle}
            style={{
              ...btnBase,
              cursor: terminalDisabled ? "default" : "pointer",
              opacity: terminalDisabled ? 0.4 : 1,
            }}
            onMouseEnter={(e) => {
              if (terminalDisabled) return;
              (e.currentTarget as HTMLButtonElement).style.color = "var(--text-primary)";
              (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)";
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLButtonElement).style.color = "var(--text-tertiary)";
              (e.currentTarget as HTMLButtonElement).style.background = "none";
            }}
          >
            <IconTerminal />
          </button>
        </div>
      </div>
      <div
        style={{
          position: "absolute",
          left: 0,
          right: 0,
          bottom: "-19px",
          height: "20px",
          background: "linear-gradient(to bottom, var(--bg-base), transparent)",
          pointerEvents: "none",
          zIndex: 1,
        }}
      />
    </div>
  );
}
