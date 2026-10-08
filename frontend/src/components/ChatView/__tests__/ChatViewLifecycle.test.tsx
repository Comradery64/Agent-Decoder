import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent, waitFor } from "@testing-library/react";
import ChatView from "../ChatView";
import { api } from "../../../api/client";
import { useAppStore } from "../../../store";

const { scrollViewport } = vi.hoisted(() => ({
  scrollViewport: {
    scrollTop: 0,
    scrollHeight: 1200,
    clientHeight: 300,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  } as unknown as HTMLElement,
}));

vi.mock("overlayscrollbars-react", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    OverlayScrollbarsComponent: React.forwardRef<
      { osInstance: () => { elements: () => { viewport: HTMLElement } } },
      { children: React.ReactNode }
    >(({ children }, ref) => {
      React.useImperativeHandle(ref, () => ({
        osInstance: () => ({
          elements: () => ({ viewport: scrollViewport }),
        }),
      }));
      return React.createElement("div", { "data-testid": "scroll-shell" }, children);
    }),
  };
});

vi.mock("../ConversationHeader", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    ConversationHeader: ({
      viewMode,
      onViewModeChange,
      nativeStateLabel,
    }: {
      viewMode?: "decoded" | "native" | "sbs";
      onViewModeChange?: (mode: "decoded" | "native" | "sbs") => void;
      nativeStateLabel?: string | null;
    }) =>
      React.createElement(
        "div",
        { "data-testid": "header" },
        nativeStateLabel
          ? React.createElement("span", { "data-testid": "native-state-badge" }, nativeStateLabel)
          : null,
        React.createElement(
          "button",
          {
            type: "button",
            "aria-pressed": viewMode === "decoded",
            onClick: () => onViewModeChange?.("decoded"),
          },
          "Decoded",
        ),
        React.createElement(
          "button",
          {
            type: "button",
            "aria-pressed": viewMode === "native",
            onClick: () => onViewModeChange?.("native"),
          },
          "Native",
        ),
        React.createElement(
          "button",
          {
            type: "button",
            "aria-pressed": viewMode === "sbs",
            onClick: () => onViewModeChange?.("sbs"),
          },
          "Split",
        ),
      ),
  };
});

vi.mock("../EmptyState", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    EmptyState: () => React.createElement("div", { "data-testid": "empty" }),
  };
});

vi.mock("../OwnershipBanner", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    OwnershipBanner: () => React.createElement("div", { "data-testid": "ownership" }),
  };
});

vi.mock("../hooks/useRecaps", () => ({
  useRecaps: () => ({
    recaps: [],
    recapGenerating: false,
    recapPromptPending: false,
    generateRecap: () => Promise.resolve(),
    dismissRecapPrompt: () => {},
    dismiss: () => {},
  }),
}));

vi.mock("../hooks/useSessionOwnership", () => ({
  useSessionOwnership: () => ({
    ownership: null,
    refetch: () => Promise.resolve(),
  }),
}));

vi.mock("../MessageListLoader", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    MessageList: ({
      optimisticActive,
      optimisticUserMessage,
    }: {
      optimisticActive: boolean;
      optimisticUserMessage?: { content: string; createdAt: number } | null;
    }) =>
      React.createElement(
        "div",
        { "data-testid": "message-list" },
        optimisticUserMessage
          ? React.createElement(
              "div",
              { "data-testid": "optimistic-user-message" },
              optimisticUserMessage.content,
            )
          : null,
        optimisticActive
          ? React.createElement("div", { "data-testid": "optimistic-active" }, "Thinking")
          : null,
      ),
  };
});

vi.mock("../NativeTerminalView", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    NativeTerminalView: ({
      sessionId,
      onNotice,
    }: {
      sessionId: string;
      onNotice?: (notice: { kind: "info" | "error"; text: string }) => void;
    }) =>
      React.createElement(
        "div",
        { "data-testid": "native-terminal-view" },
        `Native ${sessionId}`,
        React.createElement(
          "button",
          {
            type: "button",
            onClick: () => onNotice?.({ kind: "info", text: "Native PTY stopped" }),
          },
          "Emit PTY notice",
        ),
      ),
  };
});

vi.mock("../ChatInputBar", async () => {
  const React = await vi.importActual<typeof import("react")>("react");
  return {
    ChatInputBar: ({
      onSubmitStart,
      onSubmitFailed,
      btwCaptureActive,
    }: {
      onSubmitStart?: (meta?: { kind: "message" | "btw" | "slash"; content: string }) => void;
      onSubmitFailed?: (meta?: { kind: "message" | "btw" | "slash"; content: string }) => void;
      btwCaptureActive?: boolean;
    }) =>
      React.createElement(
        "div",
        null,
        btwCaptureActive
          ? React.createElement("div", { "data-testid": "btw-capture-active" }, "BTW capture active")
          : null,
        React.createElement(
          "button",
          { type: "button", onClick: () => onSubmitStart?.({ kind: "btw", content: "/btw side" }) },
          "Submit BTW",
        ),
        React.createElement(
          "button",
          { type: "button", onClick: () => onSubmitStart?.({ kind: "slash", content: "/help" }) },
          "Submit Slash",
        ),
        React.createElement(
          "button",
          { type: "button", onClick: () => onSubmitStart?.({ kind: "message", content: "first cold prompt" }) },
          "Submit Message",
        ),
        React.createElement(
          "button",
          {
            type: "button",
            onClick: () => {
              onSubmitStart?.({ kind: "slash", content: "/brief" });
              onSubmitFailed?.({ kind: "slash", content: "/brief" });
            },
          },
          "Reject Slash",
        ),
      ),
  };
});

describe("ChatView submit lifecycle events", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // Per-session view-mode memory persists in sessionStorage; clear it so a
    // mode stored by one test (all share "sess-lifecycle") can't bleed into the
    // next and override the default decoded view.
    sessionStorage.clear();
    scrollViewport.scrollTop = 0;
    Object.defineProperty(scrollViewport, "scrollHeight", {
      value: 1200,
      configurable: true,
    });
    Object.defineProperty(scrollViewport, "clientHeight", {
      value: 300,
      configurable: true,
    });
    vi.stubGlobal("ResizeObserver", class {
      observe = vi.fn();
      disconnect = vi.fn();
    });
    useAppStore.setState({ selectedSessionId: "sess-lifecycle" });
    vi.spyOn(api, "getConfig").mockResolvedValue({
      data_paths: [],
      profiles: [],
      active_profile_id: null,
      theme: "system",
      auto_open_browser: false,
      port: 4242,
      host: "127.0.0.1",
      edit_enabled: true,
      show_provider_tag: false,
      claude_default_permission_mode: "dontAsk",
      chat_send_shortcut: "enter",
      native_pty_font_family: "monaspace-argon",
      native_pty_cols: 100,
      claude_auto_stop_quiet_default_turns: false,
      claude_recap_enabled: false,
      claude_recap_idle_minutes: 10,
    });
    vi.spyOn(api, "getSession").mockResolvedValue({
      id: "sess-lifecycle",
      project_id: "project-1",
      file_path: "/tmp/session.jsonl",
      title: "Lifecycle",
      custom_title: null,
      archived_at: null,
      starred_at: null,
      viewed_at: null,
      model: null,
      started_at: null,
      updated_at: null,
      message_count: 0,
      user_message_count: 0,
      cwd: "/tmp",
      git_branch: null,
      is_worktree: false,
      is_fork: false,
      permission_mode: null,
      last_message_role: null,
      cwd_exists: true,
      messages: [],
    });
  });

  it("keeps /btw as a side-channel and never marks the main turn active", async () => {
    render(<ChatView />);

    fireEvent.click(screen.getByRole("button", { name: /submit btw/i }));

    expect(screen.queryByTestId("optimistic-active")).not.toBeInTheDocument();
    expect(screen.getByTestId("btw-capture-active")).toBeInTheDocument();

    window.dispatchEvent(
      new CustomEvent("agent-decoder:pty-submit-completed", {
        detail: {
          session_id: "sess-lifecycle",
          kind: "btw",
          status: "completed",
          input_id: 1,
          response_id: 2,
        },
      }),
    );

    await waitFor(() => {
      expect(screen.queryByTestId("optimistic-active")).not.toBeInTheDocument();
      expect(screen.queryByTestId("btw-capture-active")).not.toBeInTheDocument();
    });
  });

  it("shows a submitted message optimistically before JSONL catches up", async () => {
    render(<ChatView />);

    fireEvent.click(screen.getByRole("button", { name: /submit message/i }));

    expect(screen.getByTestId("optimistic-user-message")).toHaveTextContent("first cold prompt");
    expect(screen.getByTestId("optimistic-active")).toBeInTheDocument();
  });

  it("emits an error toast when a /btw submit times out", async () => {
    const toast = vi.fn();
    const listener = (e: Event) => toast((e as CustomEvent).detail);
    window.addEventListener("agent-decoder:toast", listener);

    render(<ChatView />);

    window.dispatchEvent(
      new CustomEvent("agent-decoder:pty-submit-completed", {
        detail: {
          session_id: "sess-lifecycle",
          kind: "btw",
          status: "timed_out",
          input_id: 1,
          response_id: null,
        },
      }),
    );

    await waitFor(() => {
      expect(toast).toHaveBeenCalledWith({
        message: expect.stringMatching(/btw.*timed out/i),
        kind: "error",
      });
    });

    window.removeEventListener("agent-decoder:toast", listener);
  });

  it("clears foreground optimistic activity when a slash command is acknowledged", async () => {
    render(<ChatView />);

    fireEvent.click(screen.getByRole("button", { name: /submit slash/i }));

    expect(screen.getByTestId("optimistic-active")).toBeInTheDocument();

    window.dispatchEvent(
      new CustomEvent("agent-decoder:pty-submit-completed", {
        detail: {
          session_id: "sess-lifecycle",
          kind: "slash",
          status: "acknowledged",
          input_id: null,
          response_id: null,
        },
      }),
    );

    await waitFor(() => {
      expect(screen.queryByTestId("optimistic-active")).not.toBeInTheDocument();
    });
  });

  it("clears foreground optimistic activity when a slash submit is rejected", async () => {
    render(<ChatView />);

    fireEvent.click(screen.getByRole("button", { name: /reject slash/i }));

    await waitFor(() => {
      expect(screen.queryByTestId("optimistic-active")).not.toBeInTheDocument();
    });
  });

  it("clears foreground optimistic activity when native PTY exits", async () => {
    render(<ChatView />);

    fireEvent.click(screen.getByRole("button", { name: /submit slash/i }));

    expect(screen.getByTestId("optimistic-active")).toBeInTheDocument();

    window.dispatchEvent(
      new CustomEvent("agent-decoder:pty-native-state", {
        detail: {
          session_id: "sess-lifecycle",
          state: "dead",
          decoded_input_safe: false,
        },
      }),
    );

    await waitFor(() => {
      expect(screen.queryByTestId("optimistic-active")).not.toBeInTheDocument();
    });
    expect(screen.queryByTestId("native-required")).not.toBeInTheDocument();
  });

  it("defaults to Decoded View", async () => {
    render(<ChatView />);

    expect(await screen.findByTestId("message-list")).toBeInTheDocument();
    expect(screen.queryByTestId("native-terminal-view")).not.toBeInTheDocument();
  });

  it("switches to Native View while keeping Decoded mounted", async () => {
    render(<ChatView />);

    expect(await screen.findByTestId("message-list")).toBeInTheDocument();
    fireEvent.click(await screen.findByRole("button", { name: "Native" }));

    expect(await screen.findByTestId("native-terminal-view")).toHaveTextContent(
      "Native sess-lifecycle",
    );
    expect(screen.getByTestId("message-list")).toBeInTheDocument();
    expect(useAppStore.getState().selectedSessionId).toBe("sess-lifecycle");
  });

  it("switches back from Native View to Decoded View", async () => {
    render(<ChatView />);

    fireEvent.click(await screen.findByRole("button", { name: "Native" }));
    expect(await screen.findByTestId("native-terminal-view")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Decoded" }));

    expect(await screen.findByTestId("message-list")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Decoded" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("native-terminal-view")).toBeInTheDocument();
  });

  it("shows both panes in Side-by-side and collapses the sidebar", async () => {
    useAppStore.setState({ sidebarCollapsed: false });
    render(<ChatView />);

    fireEvent.click(await screen.findByRole("button", { name: "Split" }));

    // Both the decoded message list and the native terminal are mounted/visible.
    expect(await screen.findByTestId("native-terminal-view")).toBeInTheDocument();
    expect(screen.getByTestId("message-list")).toBeInTheDocument();
    // Side-by-side hides the sidebar to make room.
    expect(useAppStore.getState().sidebarCollapsed).toBe(true);

    // Leaving SBS restores the prior sidebar state.
    fireEvent.click(screen.getByRole("button", { name: "Decoded" }));
    expect(useAppStore.getState().sidebarCollapsed).toBe(false);
  });

  it("shows native PTY notices in the main chat window", async () => {
    render(<ChatView />);

    fireEvent.click(await screen.findByRole("button", { name: "Native" }));
    fireEvent.click(await screen.findByRole("button", { name: /emit pty notice/i }));

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent("Native PTY stopped");
    expect(screen.getByTestId("native-terminal-view")).not.toContainElement(notice);
  });

  it("keeps the decoded composer available for generic native input state", async () => {
    render(<ChatView />);

    act(() => {
      window.dispatchEvent(
        new CustomEvent("agent-decoder:pty-native-state", {
          detail: {
            session_id: "sess-lifecycle",
            state: "native_input_required",
            decoded_input_safe: false,
          },
        }),
      );
    });

    expect(await screen.findByTestId("native-state-badge")).toHaveTextContent(
      "Native input required",
    );
    expect(screen.queryByTestId("native-required")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /submit btw/i })).toBeInTheDocument();
  });

  // As of the native-view rework, ChatView deliberately does NOT auto-switch to
  // Native on an interactive/blocking PTY state (see ChatView.tsx) — only the
  // user's explicit view choice switches modes. A blocking state is instead
  // surfaced via the header badge so the user can choose to flip to Native.
  it.each([
    ["ask_user_question", "Native input required"],
    ["permission_prompt", "Native input required"],
    ["login_required", "Claude login required"],
    ["trust_prompt", "Native input required"],
    ["btw_modal", "Native input required"],
    ["unknown_interactive", "Native input required"],
  ])(
    "surfaces blocking native state %s via the header badge without auto-switching",
    async (state, label) => {
      render(<ChatView />);

      window.dispatchEvent(
        new CustomEvent("agent-decoder:pty-native-state", {
          detail: {
            session_id: "sess-lifecycle",
            state,
            decoded_input_safe: false,
          },
        }),
      );

      expect(await screen.findByTestId("native-state-badge")).toHaveTextContent(
        label,
      );
      // No auto-switch: the Native terminal must NOT mount on its own.
      expect(
        screen.queryByTestId("native-terminal-view"),
      ).not.toBeInTheDocument();
    },
  );

  it("does not auto-switch for slash palette but shows a badge", async () => {
    render(<ChatView />);

    window.dispatchEvent(
      new CustomEvent("agent-decoder:pty-native-state", {
        detail: {
          session_id: "sess-lifecycle",
          state: "slash_palette_open",
          decoded_input_safe: false,
        },
      }),
    );

    expect(await screen.findByTestId("message-list")).toBeInTheDocument();
    expect(screen.queryByTestId("native-terminal-view")).not.toBeInTheDocument();
    expect(await screen.findByTestId("native-state-badge")).toHaveTextContent(
      "Slash menu open",
    );
  });

  it("preserves the decoded scroll position when returning after native PTY stops", async () => {
    render(<ChatView />);
    await screen.findByTestId("message-list");
    scrollViewport.scrollTop = 420;

    fireEvent.click(await screen.findByRole("button", { name: "Native" }));
    expect(await screen.findByTestId("native-terminal-view")).toBeInTheDocument();

    act(() => {
      window.dispatchEvent(
        new CustomEvent("agent-decoder:pty-native-state", {
          detail: {
            session_id: "sess-lifecycle",
            state: "dead",
            decoded_input_safe: false,
          },
        }),
      );
    });

    fireEvent.click(screen.getByRole("button", { name: "Decoded" }));

    expect(await screen.findByTestId("message-list")).toBeInTheDocument();
    expect(scrollViewport.scrollTop).toBe(420);
  });

  it("does not force bottom whenever returning from Native to Decoded", async () => {
    render(<ChatView />);
    await screen.findByTestId("message-list");
    scrollViewport.scrollTop = 360;

    fireEvent.click(await screen.findByRole("button", { name: "Native" }));
    expect(await screen.findByTestId("native-terminal-view")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Decoded" }));

    expect(await screen.findByTestId("message-list")).toBeInTheDocument();
    expect(scrollViewport.scrollTop).toBe(360);
  });
});
