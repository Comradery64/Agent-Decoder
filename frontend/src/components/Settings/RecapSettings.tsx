import type { AppConfig } from "../../api/types";
import { HINT_STYLE, sectionLabelStyle, ToggleRow } from "./shared";

export function RecapSettings({
  config,
  onChange,
}: {
  config: AppConfig;
  onChange: (updated: AppConfig) => void;
}) {
  const enabled = config.claude_recap_enabled;
  const minutes = config.claude_recap_idle_minutes;
  return (
    <div>
      <div style={sectionLabelStyle}>Auto-recap</div>
      <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
        <ToggleRow
          label="Enable auto-recap"
          checked={enabled}
          onChange={(v) => onChange({ ...config, claude_recap_enabled: v })}
        />
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: "10px",
            opacity: enabled ? 1 : 0.5,
          }}
        >
          <label style={{ fontSize: "13px", color: "var(--text-secondary)", minWidth: "100px" }}>
            Idle minutes
          </label>
          <input
            type="number"
            min={1}
            max={60}
            value={minutes}
            disabled={!enabled}
            onChange={(e) => {
              const n = parseInt(e.target.value, 10);
              if (n >= 1 && n <= 60) onChange({ ...config, claude_recap_idle_minutes: n });
            }}
            style={{
              width: "80px",
              padding: "5px 8px",
              fontSize: "13px",
              background: "var(--bg-input)",
              border: "1px solid var(--border-default)",
              borderRadius: "var(--radius-sm)",
              color: "var(--text-primary)",
              fontFamily: "var(--font-mono)",
            }}
          />
        </div>
        <div style={HINT_STYLE}>
          Generate a short Haiku-backed summary when you return to a session after being away.
          Recaps are stored in agent-decoder only — they never modify your conversation file.
        </div>
      </div>
    </div>
  );
}
