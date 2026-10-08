// Two small status banners that sit above the chat input card:
//   1. ErrorBanner — generic red banner for send-time errors.
//   2. DefaultModeBanner — orange warning when the user has selected the
//      `default` interactive permission mode (which web UI can't answer).
//
// These are presentational only; state ownership lives in ChatInput.

export function ErrorBanner({ message }: { message: string }) {
  return (
    <div
      style={{
        padding: "8px 14px",
        marginBottom: "10px",
        background: "rgba(239, 68, 68, 0.08)",
        border: "1px solid rgba(239, 68, 68, 0.35)",
        borderRadius: "12px",
        fontSize: "12px",
        color: "#b91c1c",
        lineHeight: 1.4,
      }}
    >
      {message}
    </div>
  );
}

export function DefaultModeBanner() {
  return (
    <div
      style={{
        padding: "6px 14px",
        marginBottom: "8px",
        background: "rgba(245, 158, 11, 0.10)",
        border: "1px solid rgba(245, 158, 11, 0.40)",
        borderRadius: "10px",
        fontSize: "11px",
        color: "#92400e",
        lineHeight: 1.4,
      }}
    >
      Interactive mode — the assistant will hang waiting for a permission prompt that can't be answered from the web UI. Use Stop to recover.
    </div>
  );
}

// The circular Send / Stop button at the right of the input card. Renders a
// stop square while streaming and an up-arrow otherwise.
export function SendStopButton({
  isStreaming,
  sideChannelSendAvailable = false,
  canSend,
  shortcutLabel = "Enter",
  onSend,
  onStop,
}: {
  isStreaming: boolean;
  sideChannelSendAvailable?: boolean;
  canSend: boolean;
  shortcutLabel?: string;
  onSend: () => void;
  onStop: () => void;
}) {
  if (isStreaming && !sideChannelSendAvailable) {
    return (
      <button
        type="button"
        onClick={onStop}
        title="Stop"
        aria-label="Stop"
        className="chat-send-btn"
        style={sendBtnStyle(true)}
      >
        <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
          <rect x="2" y="2" width="8" height="8" rx="1" fill="currentColor" />
        </svg>
      </button>
    );
  }
  return (
    <button
      type="button"
      onClick={onSend}
      title={`Send (${shortcutLabel})`}
      aria-label="Send message"
      className="chat-send-btn"
      style={sendBtnStyle(canSend)}
    >
      <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
        <path
          d="M7 11.5V2.5M3 6l4-4 4 4"
          stroke="currentColor"
          strokeWidth="1.75"
          strokeLinecap="round"
          strokeLinejoin="round"
          fill="none"
        />
      </svg>
    </button>
  );
}

export function SendShortcutHint({ label }: { label: string }) {
  return (
    <span
      aria-label={`Send shortcut: ${label}`}
      title={`Send shortcut: ${label}`}
      style={{
        color: "var(--text-tertiary)",
        fontFamily: "var(--font-ui)",
        fontSize: "11px",
        whiteSpace: "nowrap",
        userSelect: "none",
      }}
    >
      {label}
    </span>
  );
}

// Pill-shaped badge that signals a stashed (Ctrl/Cmd+S) draft and restores
// it on click.
export function StashBadge({ onClick }: { onClick: () => void }) {
  return (
    <button
      type="button"
      onClick={onClick}
      title="Restore stashed message"
      style={{
        background: "var(--bg-tool-block)",
        border: "1px solid var(--border-subtle)",
        borderRadius: "999px",
        color: "var(--text-secondary)",
        fontSize: "11px",
        padding: "4px 11px",
        cursor: "pointer",
        fontFamily: "var(--font-ui)",
      }}
    >
      1 stashed · click to restore
    </button>
  );
}

// Quiet disclaimer that sits under the chat input card.
export function ChatDisclaimer() {
  return (
    <div
      style={{
        textAlign: "center",
        marginTop: "10px",
        fontSize: "12px",
        color: "var(--text-tertiary)",
        fontFamily: "var(--font-ui)",
      }}
    >
      Agent Decoder is AI and can make mistakes. Please double-check responses.
    </div>
  );
}

function sendBtnStyle(active: boolean): React.CSSProperties {
  return {
    width: "32px",
    height: "32px",
    borderRadius: "999px",
    border: "none",
    cursor: active ? "pointer" : "default",
    background: active ? "var(--text-primary)" : "var(--bg-tool-block)",
    color: active ? "var(--bg-base)" : "var(--text-tertiary)",
    display: "inline-flex",
    alignItems: "center",
    justifyContent: "center",
    transition: "background var(--transition-fast)",
    flexShrink: 0,
  };
}
