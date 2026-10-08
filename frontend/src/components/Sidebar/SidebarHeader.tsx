import { useAppStore } from "../../store";
import { navigateTo } from "../../router";
import { NewTaskButton } from "./NewTaskButton";

function IconSidebarCollapse() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <rect x="3" y="3" width="18" height="18" rx="2"/>
      <path d="M9 3v18"/>
    </svg>
  );
}

function IconFolder() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
    </svg>
  );
}

function IconChat() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
    </svg>
  );
}

const btnStyle: React.CSSProperties = {
  background: "none",
  border: "none",
  cursor: "pointer",
  color: "var(--text-tertiary)",
  padding: "6px",
  borderRadius: "var(--radius-sm)",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  flexShrink: 0,
  transition: "color var(--transition-fast), background var(--transition-fast)",
};

function HeaderBtn({ children, label, onClick, active, collapsedStyle }: { children: React.ReactNode; label: string; onClick: () => void; active?: boolean; collapsedStyle?: boolean }) {
  return (
    <button
      onClick={onClick}
      aria-label={label}
      title={label}
      style={{
        ...btnStyle,
        ...(collapsedStyle ? { width: "calc(100% - 12px)", margin: "1px 6px", padding: "7px 12px" } : {}),
        color: active ? "var(--text-primary)" : "var(--text-tertiary)",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = "var(--bg-sidebar-hover)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLButtonElement).style.background = "none";
      }}
    >
      {children}
    </button>
  );
}

export function SidebarHeader({ collapsed }: { collapsed?: boolean }) {
  const toggleSidebar = useAppStore((s) => s.toggleSidebar);
  const sidebarMode = useAppStore((s) => s.sidebarMode);
  const setSidebarMode = useAppStore((s) => s.setSidebarMode);

  const toggleMode = () => {
    setSidebarMode(sidebarMode === "chat" ? "folder" : "chat");
  };

  const goHome = () => {
    navigateTo("/");
  };

  return (
    <div
      style={{
        flexShrink: 0,
        height: "var(--header-height)",
        display: "flex",
        alignItems: "center",
        padding: "0 12px",
        overflow: "hidden",
      }}
    >
      {/* Wordmark — flex:1 fills remaining space, clips when narrow */}
      <button
        onClick={goHome}
        style={{
          flex: 1,
          minWidth: 0,
          overflow: "hidden",
          fontFamily: "var(--font-ui)",
          fontSize: "18px",
          fontWeight: 600,
          color: "var(--text-primary)",
          letterSpacing: "-0.02em",
          userSelect: "none",
          whiteSpace: "nowrap",
          opacity: collapsed ? 0 : 1,
          transition: "opacity var(--transition-medium)",
          background: "none",
          border: "none",
          cursor: "pointer",
          padding: 0,
          textAlign: "left" as const,
        }}
      >
        Agent Decoder
      </button>

      {/* Action buttons — smooth max-width + opacity collapse in sync with sidebar */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          flexShrink: 0,
          overflow: "hidden",
          maxWidth: collapsed ? 0 : 80,
          opacity: collapsed ? 0 : 1,
          transition: "opacity var(--transition-medium), max-width var(--transition-medium)",
          pointerEvents: collapsed ? "none" as const : "auto" as const,
        }}
      >
        <NewTaskButton />
        <HeaderBtn
          label={sidebarMode === "chat" ? "File explorer" : "Session list"}
          onClick={toggleMode}
          active={sidebarMode === "folder"}
        >
          {sidebarMode === "chat" ? <IconFolder /> : <IconChat />}
        </HeaderBtn>
      </div>

      {/* Collapse toggle — always visible */}
      <HeaderBtn
        label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
        onClick={toggleSidebar}
        active={collapsed}
      >
        <IconSidebarCollapse />
      </HeaderBtn>
    </div>
  );
}
