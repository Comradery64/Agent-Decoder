import { describe, expect, it, vi, beforeEach } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { AppConfig } from "../../../api/types";
import SettingsModal from "../SettingsModal";
import { api } from "../../../api/client";

const baseConfig: AppConfig = {
  data_paths: [],
  profiles: [],
  active_profile_id: null,
  theme: "system",
  auto_open_browser: false,
  port: 4242,
  host: "127.0.0.1",
  edit_enabled: true,
  show_provider_tag: false,
  claude_default_permission_mode: "default",
  chat_send_shortcut: "enter",
  native_pty_font_family: "monaspace-argon",
  native_pty_cols: 100,
  claude_auto_stop_quiet_default_turns: false,
  claude_recap_enabled: false,
  claude_recap_idle_minutes: 10,
};

vi.mock("../../../api/client", () => ({
  api: {
    updateConfig: vi.fn().mockResolvedValue(undefined),
    getHostInfo: vi.fn().mockResolvedValue({
      is_remote_client: false,
      platform: "darwin",
      client_host: "127.0.0.1",
      version: "9.9.9",
    }),
    getUpdateCheck: vi.fn().mockResolvedValue({
      current_version: "9.9.9",
      latest_version: "9.9.9",
      update_available: false,
    }),
  },
  getCachedConfig: vi.fn(() => baseConfig),
  getConfigCached: vi.fn(),
}));

describe("SettingsModal", () => {
  beforeEach(() => {
    vi.mocked(api.updateConfig).mockClear();
  });

  it("lets the user switch the decoded composer send shortcut", () => {
    render(<SettingsModal />);

    // Settings is a left-rail panel: open the "Chat" category first.
    fireEvent.click(screen.getByRole("button", { name: "Chat" }));
    fireEvent.click(screen.getByRole("button", { name: "Cmd/Ctrl+Enter" }));

    expect(api.updateConfig).toHaveBeenCalledWith({
      ...baseConfig,
      chat_send_shortcut: "modEnter",
    });
  });

  it("lets the user choose the native PTY font from the preview list", () => {
    render(<SettingsModal />);

    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    // Custom listbox with per-font previews (not a <select>): click the option.
    fireEvent.click(screen.getByRole("option", { name: "Xanh Mono" }));

    expect(api.updateConfig).toHaveBeenCalledWith({
      ...baseConfig,
      native_pty_font_family: "xanh-mono",
    });
  });

  it("widens the native terminal in column steps", () => {
    render(<SettingsModal />);

    fireEvent.click(screen.getByRole("button", { name: "Terminal" }));
    fireEvent.click(screen.getByRole("button", { name: "Wider terminal" }));

    expect(api.updateConfig).toHaveBeenCalledWith({
      ...baseConfig,
      native_pty_cols: 110,
    });
  });

  it("shows the version from the backend in the About panel", async () => {
    render(<SettingsModal />);

    fireEvent.click(screen.getByRole("button", { name: "About" }));

    // Version is fetched from /api/host-info (the backend's single source) —
    // there is no version string hard-coded in the frontend.
    expect(await screen.findByText("Version 9.9.9")).toBeInTheDocument();
    expect(screen.getByText("Agent Decoder")).toBeInTheDocument();
    expect(screen.getByText("macOS")).toBeInTheDocument();
    expect(api.getHostInfo).toHaveBeenCalled();
  });

  it("does not show an update banner when already on the latest version", async () => {
    render(<SettingsModal />);
    fireEvent.click(screen.getByRole("button", { name: "About" }));

    await screen.findByText("Version 9.9.9");
    expect(screen.queryByText(/available/)).not.toBeInTheDocument();
  });

  it("shows an update banner when a newer version is on PyPI", async () => {
    vi.mocked(api.getUpdateCheck).mockResolvedValueOnce({
      current_version: "9.9.9",
      latest_version: "10.0.0",
      update_available: true,
    });
    render(<SettingsModal />);
    fireEvent.click(screen.getByRole("button", { name: "About" }));

    expect(await screen.findByText(/v10\.0\.0 available/)).toBeInTheDocument();
  });
});
