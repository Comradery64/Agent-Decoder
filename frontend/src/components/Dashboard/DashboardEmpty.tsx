export function DashboardEmpty({ onSearch }: { onSearch: () => void }) {
  return (
    <div style={{
      flex: 1,
      display: "flex",
      flexDirection: "column",
      alignItems: "center",
      justifyContent: "center",
      gap: "16px",
      padding: "60px 28px",
      textAlign: "center",
    }}>
      <div style={{
        width: "56px",
        height: "56px",
        borderRadius: "var(--radius-lg)",
        background: "var(--accent-orange-subtle)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--accent-orange)",
      }}>
        <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
        </svg>
      </div>
      <h2 style={{
        fontFamily: "var(--font-content)",
        fontSize: "22px",
        fontWeight: 600,
        margin: 0,
        color: "var(--text-primary)",
      }}>
        No sessions yet
      </h2>
      <div style={{ fontSize: "14px", color: "var(--text-secondary)", maxWidth: "440px", lineHeight: 1.6 }}>
        Agent Decoder reads your local session history. Start a session and it will appear here automatically.
      </div>
      <button
        onClick={onSearch}
        style={{
          marginTop: "8px",
          padding: "8px 16px",
          background: "var(--accent-orange)",
          color: "var(--text-on-accent)",
          border: "none",
          borderRadius: "var(--radius-md)",
          cursor: "pointer",
          fontSize: "13px",
          fontWeight: 500,
        }}
      >
        Open search
      </button>
    </div>
  );
}
