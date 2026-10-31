import { act, render, screen, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { NativeTerminalView } from "../NativeTerminalView";
import { ProviderThemeProvider, defaultCapsFor } from "../ProviderThemeContext";
import { api } from "../../../api/client";
import { emit } from "../../../utils/events";

const { fitDimensions, configSource, fitAddonInstances, terminalInstances, MockFitAddon, MockTerminal } = vi.hoisted(() => {
  const fitDimensions = {
    current: { cols: 100, rows: 36 } as { cols: number; rows: number } | undefined,
  };
  const configSource = {
    current: {
      data_paths: [],
      profiles: [],
      active_profile_id: null,
      theme: "system",
      auto_open_browser: false,
      port: 4242,
      host: "127.0.0.1",
      edit_enabled: true,
      claude_default_permission_mode: "default",
      chat_send_shortcut: "enter",
      native_pty_font_family: "monaspace-argon",
      native_pty_cols: 100,
      claude_auto_stop_quiet_default_turns: false,
      claude_recap_enabled: false,
      claude_recap_idle_minutes: 5,
    },
  };

  class MockFitAddon {
    activate = vi.fn();
    dispose = vi.fn();
    proposeDimensions = vi.fn(() => fitDimensions.current);

    constructor() {
      fitAddonInstances.push(this);
    }
  }

  class MockTerminal {
    options: Record<string, unknown>;
    cols = 100;
    rows = 40;
    writes: Array<string | Uint8Array> = [];
    dataHandler: ((data: string) => void) | null = null;
    open = vi.fn();
    clear = vi.fn();
    scrollToBottom = vi.fn();
    dispose = vi.fn();
    write = vi.fn((data: string | Uint8Array, callback?: () => void) => {
      this.writes.push(data);
      // xterm's write(data, cb) fires cb after the chunk is parsed — the
      // chunked snapshot replay relies on it to advance through the ring.
      callback?.();
    });
    loadAddon = vi.fn((addon: { activate?: (terminal: MockTerminal) => void }) => {
      addon.activate?.(this);
    });
    resize = vi.fn((cols: number, rows: number) => {
      this.cols = cols;
      this.rows = rows;
    });
    onData = vi.fn((handler: (data: string) => void) => {
      this.dataHandler = handler;
      return { dispose: vi.fn() };
    });
    wheelHandler: ((e: WheelEvent) => boolean) | null = null;
    buffer = { active: { type: "normal" as "normal" | "alternate" } };
    attachCustomWheelEventHandler = vi.fn((handler: (e: WheelEvent) => boolean) => {
      this.wheelHandler = handler;
    });
    // xterm exposes a unicode service; xtermTerminal.ts sets activeVersion and
    // the Unicode11 addon registers a provider against it.
    unicode = { activeVersion: "6", register: vi.fn() };
    // createNativeTerminal registers a CSI handler (ESC[3J guard) on the parser.
    parser = { registerCsiHandler: vi.fn() };

    constructor(options: Record<string, unknown>) {
      this.options = options;
      terminalInstances.push(this);
    }
  }
  const fitAddonInstances: MockFitAddon[] = [];
  const terminalInstances: MockTerminal[] = [];
  return { fitDimensions, configSource, fitAddonInstances, terminalInstances, MockFitAddon, MockTerminal };
});

vi.mock("@xterm/xterm", () => ({ Terminal: MockTerminal }));
vi.mock("@xterm/addon-fit", () => ({ FitAddon: MockFitAddon }));
vi.mock("@xterm/addon-unicode11", () => ({
  Unicode11Addon: class {
    activate = vi.fn();
    dispose = vi.fn();
  },
}));
vi.mock("@xterm/xterm/css/xterm.css", () => ({}));

vi.mock("../../../api/client", () => ({
  api: {
    ptyNativeSnapshot: vi.fn(),
    ptyFocus: vi.fn(),
    ptyInput: vi.fn(),
    ptyResize: vi.fn(),
    ptyBlur: vi.fn(),
    ptyKillKeepalive: vi.fn(),
  },
  getCachedConfig: vi.fn(() => configSource.current),
  getConfigCached: vi.fn(() => Promise.resolve(configSource.current)),
}));

function bytesToText(data: string | Uint8Array): string {
  if (typeof data === "string") return data;
  return new TextDecoder().decode(data);
}

function textToBase64(text: string): string {
  return btoa(text);
}

describe("NativeTerminalView", () => {
  let resizeObserverCallbacks: Array<() => void> = [];

  beforeEach(() => {
    vi.useRealTimers();
    vi.clearAllMocks();
    fitDimensions.current = { cols: 100, rows: 36 };
    configSource.current = {
      ...configSource.current,
      native_pty_font_family: "monaspace-argon",
      native_pty_cols: 100,
    };
    fitAddonInstances.length = 0;
    terminalInstances.length = 0;
    resizeObserverCallbacks = [];
    vi.stubGlobal("ResizeObserver", class {
      constructor(callback: () => void) {
        resizeObserverCallbacks.push(callback);
      }
      observe = vi.fn();
      disconnect = vi.fn();
    });
    document.documentElement.style.setProperty("--bg-base", "#faf9f5");
    document.documentElement.style.setProperty("--text-primary", "#484846");
    document.documentElement.style.setProperty("--accent-orange-subtle", "rgba(139, 115, 85, 0.08)");
    vi.mocked(api.ptyNativeSnapshot).mockResolvedValue({
      session_id: "sess-native",
      ring_b64: textToBase64("hello"),
      rows: 36,
      cols: 100,
      alive: true,
      native_state: "idle_chat_input",
      decoded_input_safe: true,
    });
    vi.mocked(api.ptyInput).mockResolvedValue({ ok: true });
    vi.mocked(api.ptyResize).mockResolvedValue({ ok: true });
    vi.mocked(api.ptyFocus).mockResolvedValue({ ok: true });
    vi.mocked(api.ptyBlur).mockResolvedValue({ ok: true });
    vi.mocked(api.ptyKillKeepalive).mockReturnValue(true);
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it("creates an xterm terminal with the configured width, font, and decoded theme", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);

    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    expect(terminalInstances[0].options).toMatchObject({
      fontSize: 13,
      scrollback: expect.any(Number),
      fontFamily: expect.stringContaining("Monaspace Argon"),
      theme: expect.objectContaining({ background: "#faf9f5", foreground: "#484846" }),
    });
    expect(screen.getByTestId("native-terminal-view")).toHaveStyle({
      background: "var(--bg-base)",
    });
  });

  it("mounts the terminal into the native PTY host element", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());
    expect(screen.getByTestId("native-terminal-host")).toBeInTheDocument();
    expect(terminalInstances[0].open).toHaveBeenCalled();
  });

  it("pins the terminal to the configured width and tracks rows from the fit addon", async () => {
    fitDimensions.current = { cols: 250, rows: 36 };
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());
    // cols pinned to native_pty_cols (100), NOT the fit's 250; rows tracked.
    expect(terminalInstances[0].resize).toHaveBeenCalledWith(100, 36);
  });

  it("sends an initial resize to the backend at the pinned width", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(api.ptyResize).toHaveBeenCalled());
    expect(api.ptyResize).toHaveBeenCalledWith("sess-native", 36, 100);
  });

  it("replays the snapshot ring and scrolls to the bottom", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => {
      expect(terminalInstances[0].writes.map(bytesToText).join("")).toContain("hello");
    });
    expect(terminalInstances[0].scrollToBottom).toHaveBeenCalled();
  });

  it("does NOT replay a snapshot belonging to a different session", async () => {
    // Reproduces the cross-session bleed: on a switch the snapshot can briefly
    // carry the PREVIOUS session's id (state lags the sessionId prop by one
    // commit). That stale snapshot must never be written into the new terminal.
    vi.mocked(api.ptyNativeSnapshot).mockResolvedValue({
      session_id: "some-other-session",
      ring_b64: textToBase64("STALE-OTHER-SESSION"),
      rows: 36,
      cols: 100,
      alive: true,
      native_state: "idle_chat_input",
      decoded_input_safe: true,
    });

    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());
    await waitFor(() => expect(api.ptyResize).toHaveBeenCalled());
    // Give the snapshot-write effect a chance to (wrongly) fire.
    await new Promise((r) => setTimeout(r, 50));

    expect(terminalInstances[0].writes.map(bytesToText).join("")).not.toContain(
      "STALE-OTHER-SESSION",
    );
  });

  it("writes incoming live pty-output-chunk events", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());
    await waitFor(() => expect(api.ptyResize).toHaveBeenCalled());

    emit("pty-output-chunk", { session_id: "sess-native", data_b64: textToBase64("world") });
    await waitFor(() => {
      expect(terminalInstances[0].writes.map(bytesToText).join("")).toContain("world");
    });
  });

  it("forwards terminal input to api.ptyInput (coalesced per animation frame)", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]?.dataHandler).toBeTruthy());
    // Input is buffered and flushed on requestAnimationFrame so a burst of
    // mouse/wheel events becomes one POST, not one per event. A single keystroke
    // therefore lands on the next frame rather than synchronously.
    terminalInstances[0].dataHandler?.("x");
    await waitFor(() => expect(api.ptyInput).toHaveBeenCalledWith("sess-native", "x"));
  });

  it("coalesces a burst of input events into a single api.ptyInput POST", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]?.dataHandler).toBeTruthy());
    vi.mocked(api.ptyInput).mockClear();
    // Several events within the same frame (the mouse-flood-scroll case).
    terminalInstances[0].dataHandler?.("\x1b[<64;1;1M");
    terminalInstances[0].dataHandler?.("\x1b[<64;1;2M");
    terminalInstances[0].dataHandler?.("\x1b[<64;1;3M");
    await waitFor(() => expect(api.ptyInput).toHaveBeenCalledTimes(1));
    expect(api.ptyInput).toHaveBeenCalledWith(
      "sess-native",
      "\x1b[<64;1;1M\x1b[<64;1;2M\x1b[<64;1;3M",
    );
  });

  it("applies theme changes in place without recreating the terminal", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    document.documentElement.style.setProperty("--bg-base", "#262624");
    act(() => {
      document.documentElement.setAttribute("data-theme", "dark");
    });

    await waitFor(() => {
      expect((terminalInstances[0].options.theme as { background?: string })?.background).toBe("#262624");
    });
    // Still exactly one terminal — no remount.
    expect(terminalInstances).toHaveLength(1);
  });

  it("applies a font change from config in place", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    act(() => {
      emit("config-updated", { ...configSource.current, native_pty_font_family: "fira-code" } as never);
    });

    await waitFor(() => {
      expect(terminalInstances[0].options.fontFamily as string).toContain("Fira Code");
    });
    expect(terminalInstances).toHaveLength(1);
  });

  it("emits a notice when the native PTY has stopped", async () => {
    const onNotice = vi.fn();
    render(<NativeTerminalView sessionId="sess-native" onNotice={onNotice} />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    act(() => {
      emit("pty-native-state", { session_id: "sess-native", state: "dead", decoded_input_safe: false });
    });

    await waitFor(() => {
      expect(onNotice).toHaveBeenCalledWith(
        expect.objectContaining({ kind: "info", text: "Native PTY stopped" }),
      );
    });
  });

  it("renders the native terminal region", () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    expect(screen.getByTestId("native-terminal-view")).toBeInTheDocument();
    expect(screen.getByTestId("native-terminal-host")).toBeInTheDocument();
  });

  it("blurs on unmount and kills the keepalive on page close", async () => {
    const { unmount } = render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    window.dispatchEvent(new Event("pagehide"));
    expect(api.ptyKillKeepalive).toHaveBeenCalledWith("sess-native");

    unmount();
    expect(api.ptyBlur).toHaveBeenCalledWith("sess-native");
  });

  it("warns before closing a live Codex session (notice + beforeunload guard)", async () => {
    const onNotice = vi.fn();
    render(
      <ProviderThemeProvider value={{ provider: "codex", caps: defaultCapsFor("codex") }}>
        <NativeTerminalView sessionId="sess-native" onNotice={onNotice} />
      </ProviderThemeProvider>,
    );
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    await waitFor(() => {
      expect(onNotice).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "info",
          text: expect.stringContaining("stops the running Codex process"),
        }),
      );
    });

    const evt = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(evt);
    expect(evt.defaultPrevented).toBe(true);
  });

  it("also warns before closing a live Claude native session (all harnesses)", async () => {
    const onNotice = vi.fn();
    // No provider wrapper → defaults to "claude".
    render(<NativeTerminalView sessionId="sess-native" onNotice={onNotice} />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());

    await waitFor(() => {
      expect(onNotice).toHaveBeenCalledWith(
        expect.objectContaining({
          kind: "info",
          text: expect.stringContaining("stops the running Claude process"),
        }),
      );
    });

    const evt = new Event("beforeunload", { cancelable: true });
    window.dispatchEvent(evt);
    expect(evt.defaultPrevented).toBe(true);
  });

  it("on the alternate buffer, wheel scrolls via Shift+Up/Down (not arrow history)", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());
    const term = terminalInstances[0];
    await waitFor(() => expect(term.wheelHandler).toBeTruthy());
    term.buffer.active.type = "alternate";

    // Wheel up by 100px → 3 lines of Shift+Up; handler cancels xterm default.
    const ret = term.wheelHandler!({ deltaY: -100, preventDefault: () => {} } as unknown as WheelEvent);
    expect(ret).toBe(false);
    await waitFor(() => {
      expect(api.ptyInput).toHaveBeenCalledWith("sess-native", "\x1b[1;2A".repeat(3));
    });

    // Wheel down → Shift+Down.
    term.wheelHandler!({ deltaY: 100, preventDefault: () => {} } as unknown as WheelEvent);
    await waitFor(() => {
      expect(api.ptyInput).toHaveBeenCalledWith("sess-native", "\x1b[1;2B".repeat(3));
    });
  });

  it("on the normal buffer, wheel defers to native xterm scrollback (no input sent)", async () => {
    render(<NativeTerminalView sessionId="sess-native" />);
    await waitFor(() => expect(terminalInstances[0]).toBeTruthy());
    const term = terminalInstances[0];
    await waitFor(() => expect(term.wheelHandler).toBeTruthy());
    term.buffer.active.type = "normal";
    vi.mocked(api.ptyInput).mockClear();

    const ret = term.wheelHandler!({ deltaY: -100, preventDefault: () => {} } as unknown as WheelEvent);
    expect(ret).toBe(true);
    await new Promise((r) => requestAnimationFrame(() => r(null)));
    expect(api.ptyInput).not.toHaveBeenCalled();
  });
});
