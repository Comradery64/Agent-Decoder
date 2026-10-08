import { useState, useEffect, type ReactNode } from "react";
import type { AppConfig, HostInfo, UpdateCheck } from "../../api/types";
import { api, getCachedConfig, getConfigCached } from "../../api/client";
import { useAppStore } from "../../store";
import { PathEditor } from "./PathEditor";
import { ScrollContainer } from "../ScrollContainer";
import { ProfileSection } from "./ProfileSection";
import { SortOrderSection } from "./SortOrderSection";
import { RecapSettings } from "./RecapSettings";
import { RescanButton } from "./RescanButton";
import { PermissionModeSection } from "./PermissionModeSection";
import { sectionLabelStyle, segmentBtnStyle, ToggleRow } from "./shared";
import { NativePtyFontPicker } from "./NativePtyFontPicker";

// Native PTY width bounds (terminal columns). Mirrors AppConfig.native_pty_cols
// (ge=20, le=400) on the backend; the stepper nudges by a comfortable amount.
const NATIVE_PTY_MIN_COLS = 20;
const NATIVE_PTY_MAX_COLS = 400;
const NATIVE_PTY_COLS_STEP = 10;

type CategoryId = "general" | "appearance" | "chat" | "terminal" | "server" | "about";

const CATEGORIES: { id: CategoryId; label: string }[] = [
  { id: "general", label: "General" },
  { id: "appearance", label: "Appearance" },
  { id: "chat", label: "Chat" },
  { id: "terminal", label: "Terminal" },
  { id: "server", label: "Server" },
  { id: "about", label: "About" },
];

const PLATFORM_LABELS: Record<string, string> = {
  darwin: "macOS",
  linux: "Linux",
  win32: "Windows",
};

export default function SettingsModal() {
  const closeSettings = useAppStore((s) => s.closeSettings);
  const showParentFolder = useAppStore((s) => s.showParentFolder);
  const setShowParentFolder = useAppStore((s) => s.setShowParentFolder);
  const setShowProviderTag = useAppStore((s) => s.setShowProviderTag);
  // Seed from the boot-time config fetch (warmed by App.tsx for theme). If the
  // cache hasn't populated yet — e.g. opened before the boot fetch resolved —
  // fall back to the deduped fetcher so we don't fire a second call.
  const [config, setConfig] = useState<AppConfig | null>(getCachedConfig);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [category, setCategory] = useState<CategoryId>("general");
  // Version + platform for the About panel. Fetched lazily when About opens.
  // The version is the backend's single source of truth (agent_decoder.__version__)
  // surfaced via /api/host-info — there is no version string in the frontend.
  const [hostInfo, setHostInfo] = useState<HostInfo | null>(null);
  // Best-effort PyPI check (see api.getUpdateCheck) — null while loading or
  // if the check failed; the banner just doesn't render either way.
  const [updateCheck, setUpdateCheck] = useState<UpdateCheck | null>(null);

  useEffect(() => {
    if (config) return;
    getConfigCached()
      .then(setConfig)
      .catch((err: unknown) => {
        setLoadError(err instanceof Error ? err.message : "Failed to load config");
      });
  }, [config]);

  useEffect(() => {
    if (category !== "about" || hostInfo) return;
    api.getHostInfo().then(setHostInfo).catch(() => {});
    api.getUpdateCheck().then(setUpdateCheck).catch(() => {});
  }, [category, hostInfo]);

  function save(updated: AppConfig) {
    setConfig(updated);
    api.updateConfig(updated).catch(() => {});
  }

  // Close on Escape.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") closeSettings();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [closeSettings]);

  return (
    // Non-blocking: the full-screen wrapper has pointer-events:none so clicks
    // pass through to the app behind (chat stays usable, no dimming). Only the
    // floating window itself is interactive. Closes via the × or Escape.
    <div
      role="dialog"
      aria-label="Settings"
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 1000,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        padding: "16px",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          pointerEvents: "auto",
          width: "100%",
          maxWidth: "760px",
          height: "min(600px, calc(100vh - 96px))",
          background: "var(--bg-modal)",
          borderRadius: "var(--radius-lg)",
          border: "1px solid var(--border-default)",
          boxShadow: "var(--shadow-lg)",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        {/* Title bar */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
            padding: "14px 16px",
            borderBottom: "1px solid var(--border-subtle)",
            flexShrink: 0,
          }}
        >
          <h2 style={{ margin: 0, fontSize: "15px", fontWeight: 600, color: "var(--text-primary)", letterSpacing: "-0.005em" }}>
            Settings
          </h2>
          <button
            onClick={closeSettings}
            aria-label="Close settings"
            style={{
              background: "none", border: "none", cursor: "pointer", color: "var(--text-tertiary)",
              fontSize: "20px", lineHeight: 1, padding: "4px",
              borderRadius: "var(--radius-sm)", transition: "color var(--transition-fast)",
            }}
            onMouseEnter={(e) => { (e.currentTarget as HTMLButtonElement).style.color = "var(--text-primary)"; }}
            onMouseLeave={(e) => { (e.currentTarget as HTMLButtonElement).style.color = "var(--text-tertiary)"; }}
          >
            ×
          </button>
        </div>

        {/* Body: left category rail + right content pane */}
        <div style={{ display: "flex", flex: 1, minHeight: 0 }}>
          <nav
            aria-label="Settings categories"
            style={{
              flexShrink: 0,
              width: "168px",
              borderRight: "1px solid var(--border-subtle)",
              padding: "8px",
              display: "flex",
              flexDirection: "column",
              gap: "2px",
              background: "var(--bg-subtle)",
              overflowY: "auto",
            }}
          >
            {CATEGORIES.map((c) => {
              const active = c.id === category;
              return (
                <button
                  key={c.id}
                  type="button"
                  aria-pressed={active}
                  onClick={() => setCategory(c.id)}
                  style={{
                    textAlign: "left",
                    padding: "7px 10px",
                    fontSize: "13px",
                    border: "none",
                    borderRadius: "var(--radius-sm)",
                    cursor: "pointer",
                    fontFamily: "var(--font-ui)",
                    background: active ? "var(--bg-base)" : "transparent",
                    color: active ? "var(--text-primary)" : "var(--text-secondary)",
                    fontWeight: active ? 600 : 400,
                  }}
                >
                  {c.label}
                </button>
              );
            })}
          </nav>

          <ScrollContainer style={{ flex: 1, padding: "18px 22px" }}>
            {loadError && (
              <div style={{
                padding: "12px", background: "var(--tool-error-bg)",
                border: "1px solid var(--tool-error-border)", borderRadius: "var(--radius-sm)",
                fontSize: "13px", color: "var(--tool-error-border)", marginBottom: "16px",
              }}>
                {loadError}
              </div>
            )}

            {!config && !loadError && (
              <div style={{ textAlign: "center", padding: "24px", fontSize: "13px", color: "var(--text-tertiary)" }}>
                Loading…
              </div>
            )}

            {config && (
              <div style={{ display: "flex", flexDirection: "column", gap: "22px" }}>
                {category === "general" && (
                  <>
                    <ProfileSection config={config} onConfigChange={setConfig} />
                    {config.profiles.length === 0 && <PathEditor config={config} onConfigChange={setConfig} />}
                    <RescanButton />
                    <ToggleRow
                      label="Open browser on startup"
                      checked={config.auto_open_browser}
                      onChange={(v) => save({ ...config, auto_open_browser: v })}
                    />
                  </>
                )}

                {category === "appearance" && (
                  <>
                    <div>
                      <div style={sectionLabelStyle}>Theme</div>
                      <div style={{ display: "flex", gap: "8px" }}>
                        {(["light", "dark", "system"] as const).map((theme) => (
                          <button
                            key={theme}
                            onClick={() => {
                              const updated = { ...config, theme };
                              setConfig(updated);
                              api.updateConfig(updated).catch(() => {});
                              const applyTheme = (
                                window as Window & { __agentDecoderApplyTheme?: (t: string) => void }
                              ).__agentDecoderApplyTheme;
                              if (applyTheme) applyTheme(theme);
                            }}
                            style={segmentBtnStyle(config.theme === theme)}
                          >
                            {theme}
                          </button>
                        ))}
                      </div>
                    </div>
                    <SortOrderSection />
                    <ToggleRow
                      label="Show parent folder in project names"
                      checked={showParentFolder}
                      onChange={setShowParentFolder}
                    />
                    <ToggleRow
                      label="Show provider tag on chats"
                      hint="Adds a small label (e.g. GPT) to non-Claude sessions in the sidebar"
                      checked={config?.show_provider_tag ?? false}
                      onChange={(v) => {
                        setShowProviderTag(v);
                        if (config) save({ ...config, show_provider_tag: v });
                      }}
                    />
                  </>
                )}

                {category === "chat" && (
                  <>
                    <ToggleRow
                      label="Allow editing & deleting messages"
                      hint="Creates a backup before every write"
                      checked={config.edit_enabled}
                      onChange={(v) => save({ ...config, edit_enabled: v })}
                    />
                    <div>
                      <div style={{ fontSize: "13px", color: "var(--text-primary)", marginBottom: "6px" }}>
                        Send shortcut
                      </div>
                      <div style={{ display: "flex", gap: "8px", flexWrap: "wrap" }}>
                        {[
                          { value: "enter", label: "Enter" },
                          { value: "modEnter", label: "Cmd/Ctrl+Enter" },
                        ].map((option) => {
                          const active = (config.chat_send_shortcut ?? "enter") === option.value;
                          return (
                            <button
                              key={option.value}
                              type="button"
                              onClick={() => save({
                                ...config,
                                chat_send_shortcut: option.value as AppConfig["chat_send_shortcut"],
                              })}
                              style={{ ...segmentBtnStyle(active), textTransform: "none" }}
                            >
                              {option.label}
                            </button>
                          );
                        })}
                      </div>
                      <div style={{ fontSize: "12px", color: "var(--text-tertiary)", marginTop: "6px", lineHeight: 1.4 }}>
                        Applies to Decoded View. Native View keeps terminal keyboard behavior.
                      </div>
                    </div>
                    <PermissionModeSection config={config} onChange={save} />
                    <RecapSettings config={config} onChange={save} />
                  </>
                )}

                {category === "terminal" && (
                  <>
                    <div>
                      <div style={{ fontSize: "13px", color: "var(--text-primary)", marginBottom: "6px" }}>
                        PTY font
                      </div>
                      <NativePtyFontPicker
                        value={config.native_pty_font_family ?? "monaspace-argon"}
                        onChange={(v) => save({ ...config, native_pty_font_family: v })}
                      />
                    </div>
                    <div>
                      <div style={{ fontSize: "13px", color: "var(--text-primary)", marginBottom: "6px" }}>
                        Terminal width
                      </div>
                      <div style={{ display: "flex", alignItems: "center", gap: "8px" }}>
                        <button
                          type="button"
                          aria-label="Narrower terminal"
                          disabled={config.native_pty_cols <= NATIVE_PTY_MIN_COLS}
                          onClick={() => save({
                            ...config,
                            native_pty_cols: Math.max(NATIVE_PTY_MIN_COLS, config.native_pty_cols - NATIVE_PTY_COLS_STEP),
                          })}
                          style={{
                            ...segmentBtnStyle(false), width: "34px", padding: "6px 0", textTransform: "none",
                            opacity: config.native_pty_cols <= NATIVE_PTY_MIN_COLS ? 0.45 : 1,
                          }}
                        >
                          −
                        </button>
                        <div
                          aria-label="Terminal width columns"
                          style={{ minWidth: "86px", textAlign: "center", fontSize: "13px", color: "var(--text-secondary)", fontFamily: "var(--font-mono)" }}
                        >
                          {config.native_pty_cols} cols
                        </div>
                        <button
                          type="button"
                          aria-label="Wider terminal"
                          disabled={config.native_pty_cols >= NATIVE_PTY_MAX_COLS}
                          onClick={() => save({
                            ...config,
                            native_pty_cols: Math.min(NATIVE_PTY_MAX_COLS, config.native_pty_cols + NATIVE_PTY_COLS_STEP),
                          })}
                          style={{
                            ...segmentBtnStyle(false), width: "34px", padding: "6px 0", textTransform: "none",
                            opacity: config.native_pty_cols >= NATIVE_PTY_MAX_COLS ? 0.45 : 1,
                          }}
                        >
                          +
                        </button>
                      </div>
                      <div style={{ fontSize: "12px", color: "var(--text-tertiary)", marginTop: "6px", lineHeight: 1.4 }}>
                        Column width Claude wraps to in Native View. Applies to new sessions; the view renders at this width and never reflows.
                      </div>
                    </div>
                  </>
                )}

                {category === "server" && (
                  <div>
                    <div style={sectionLabelStyle}>
                      Server
                      <span style={{ fontSize: "10px", color: "var(--text-tertiary)", fontWeight: 400, marginLeft: "6px", textTransform: "none", letterSpacing: 0 }}>
                        requires restart
                      </span>
                    </div>
                    <div style={{ display: "flex", flexDirection: "column", gap: "10px" }}>
                      <div style={{ display: "flex", alignItems: "center", gap: "10px" }}>
                        <label style={{ fontSize: "13px", color: "var(--text-secondary)", minWidth: "80px" }}>Port</label>
                        <input
                          type="number"
                          min={1024}
                          max={65535}
                          value={config.port}
                          onChange={(e) => {
                            const port = parseInt(e.target.value, 10);
                            if (port >= 1024 && port <= 65535) save({ ...config, port });
                          }}
                          style={{
                            width: "90px", padding: "5px 8px", fontSize: "13px",
                            background: "var(--bg-input)", border: "1px solid var(--border-default)",
                            borderRadius: "var(--radius-sm)", color: "var(--text-primary)", fontFamily: "var(--font-mono)",
                          }}
                        />
                      </div>
                      <ToggleRow
                        label="Expose on local network"
                        hint="Binds to 0.0.0.0 — anyone on your network can view your data"
                        checked={config.host === "0.0.0.0"}
                        onChange={(v) => save({ ...config, host: v ? "0.0.0.0" : "127.0.0.1" })}
                        danger={config.host === "0.0.0.0"}
                      />
                    </div>
                  </div>
                )}

                {category === "about" && (
                  <AboutPanel hostInfo={hostInfo} updateCheck={updateCheck} />
                )}
              </div>
            )}
          </ScrollContainer>
        </div>
      </div>
    </div>
  );
}

const ABOUT_REPO = "https://github.com/Comradery64/Agent-Decoder";

function AboutLink({ href, children }: { href: string; children: ReactNode }) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noreferrer"
      style={{ color: "var(--text-secondary)", textDecoration: "none", transition: "color var(--transition-fast)" }}
      onMouseEnter={(e) => { (e.currentTarget as HTMLAnchorElement).style.color = "var(--text-accent)"; }}
      onMouseLeave={(e) => { (e.currentTarget as HTMLAnchorElement).style.color = "var(--text-secondary)"; }}
    >
      {children}
    </a>
  );
}

// Apple "About This Mac"–style panel: centered app mark, name, version, a short
// description, and links. The version comes straight from /api/host-info (the
// backend's single source of truth), so nothing here ever needs hand-editing.
function AboutPanel({
  hostInfo,
  updateCheck,
}: {
  hostInfo: HostInfo | null;
  updateCheck: UpdateCheck | null;
}) {
  const platform = hostInfo ? (PLATFORM_LABELS[hostInfo.platform] ?? hostInfo.platform) : null;
  const dot = <span style={{ color: "var(--border-default)" }}>·</span>;
  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        alignItems: "center",
        textAlign: "center",
        padding: "20px 16px 8px",
      }}
    >
      {/* App mark — the rounded-square icon already carries its own corners
          and soft shadow, so it's rendered as-is (served from public/). */}
      <img
        src="/app-icon.png"
        alt="Agent Decoder"
        width={72}
        height={72}
        style={{ display: "block", marginBottom: "14px" }}
      />

      <div style={{ fontSize: "26px", fontWeight: 600, letterSpacing: "-0.02em", color: "var(--text-primary)", lineHeight: 1.1 }}>
        Agent Decoder
      </div>

      <div style={{ fontSize: "13px", color: "var(--text-secondary)", marginTop: "8px" }}>
        Version {hostInfo?.version ?? "…"}
      </div>
      {platform && (
        <div style={{ fontSize: "12px", color: "var(--text-tertiary)", marginTop: "2px" }}>
          {platform}
        </div>
      )}

      {updateCheck?.update_available && (
        <a
          href={`${ABOUT_REPO}/blob/main/CHANGELOG.md`}
          target="_blank"
          rel="noreferrer"
          style={{
            display: "block",
            marginTop: "12px",
            padding: "6px 12px",
            borderRadius: "6px",
            background: "var(--accent-orange-subtle)",
            color: "var(--accent-orange)",
            fontSize: "12px",
            fontWeight: 500,
            textDecoration: "none",
          }}
        >
          v{updateCheck.latest_version} available — run{" "}
          <code style={{ fontFamily: "var(--font-mono)" }}>pipx install --force agent-decoder</code> to update
        </a>
      )}

      <p
        style={{
          fontSize: "12.5px",
          color: "var(--text-tertiary)",
          lineHeight: 1.55,
          maxWidth: "340px",
          margin: "18px 0 0",
        }}
      >
        Browse, search, and analyze your AI coding assistant chat history —
        entirely local, entirely private.
      </p>

      <div style={{ display: "flex", alignItems: "center", gap: "10px", marginTop: "16px", fontSize: "12px" }}>
        <AboutLink href={ABOUT_REPO}>GitHub</AboutLink>
        {dot}
        <AboutLink href={`${ABOUT_REPO}/blob/main/CHANGELOG.md`}>Changelog</AboutLink>
        {dot}
        <AboutLink href={`${ABOUT_REPO}/blob/main/LICENSE`}>License</AboutLink>
      </div>

      <div style={{ fontSize: "11px", color: "var(--text-tertiary)", marginTop: "20px", letterSpacing: "0.01em" }}>
        FSL-1.1-Apache-2.0
      </div>
    </div>
  );
}
