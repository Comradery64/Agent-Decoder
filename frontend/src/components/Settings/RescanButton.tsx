import { useState } from "react";
import { api } from "../../api/client";
import { HINT_STYLE } from "./shared";

export function RescanButton() {
  const [rescanStatus, setRescanStatus] = useState<"idle" | "scanning" | "done">("idle");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: "6px", marginTop: "2px" }}>
      <button
        onClick={async () => {
          setRescanStatus("scanning");
          try { await api.refresh(); } catch { /* ignore */ }
          setRescanStatus("done");
          setTimeout(() => setRescanStatus("idle"), 2000);
        }}
        disabled={rescanStatus === "scanning"}
        style={{
          alignSelf: "flex-start",
          padding: "6px 14px",
          fontSize: "13px",
          background: "var(--bg-tool-block)",
          color: rescanStatus === "done" ? "var(--accent-orange)" : "var(--text-secondary)",
          border: "1px solid",
          borderColor: rescanStatus === "done" ? "var(--accent-orange)" : "var(--border-subtle)",
          borderRadius: "var(--radius-sm)",
          cursor: rescanStatus === "scanning" ? "default" : "pointer",
          fontFamily: "var(--font-ui)",
          transition: "all var(--transition-fast)",
        }}
      >
        {rescanStatus === "scanning" ? "Scanning…" : rescanStatus === "done" ? "Done ✓" : "Force rescan"}
      </button>
      <div style={HINT_STYLE}>
        Re-reads every configured data path for new or changed sessions. Use this if you edited a session JSONL outside agent-decoder, or if a session you expect to see isn't appearing.
      </div>
    </div>
  );
}
