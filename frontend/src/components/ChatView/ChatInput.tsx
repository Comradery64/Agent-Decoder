import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../../api/client";
import type { ChatSendShortcut, PermissionMode } from "../../api/types";
import { ModelPicker, type ModelId } from "./ModelPicker";
import {
  ErrorBanner,
  SendStopButton,
  SendShortcutHint,
  StashBadge,
  ChatDisclaimer,
} from "./ChatInputBanners";
import { useMessageHistory } from "./hooks/useMessageHistory";
import { useProviderTheme } from "./ProviderThemeContext";
import { markClauDecodeSubmit } from "../../utils/localStorage";

export type SubmitKind = "message" | "btw" | "slash";
export interface SubmitMeta {
  kind: SubmitKind;
  content: string;
}

interface ChatInputProps {
  sessionId: string;
  isStreaming: boolean;
  disabled?: boolean;
  onStop: () => void;
  // Called inside handleSend the instant validation passes (before any
  // network I/O), so the parent can flip its optimistic "Thinking" flag
  // immediately. Keeps the indicator responsive even when PTY submit
  // latency or SSE lag would otherwise delay it.
  onSubmitStart?: (meta?: SubmitMeta) => void;
  onSubmitFailed?: (meta?: SubmitMeta) => void;
  // True when the session's working directory no longer exists on disk
  // (derived from SessionDetail.cwd_exists). handleSend short-circuits
  // with a clear error rather than spending the spawn cycle on a doomed
  // ``claude --cwd <missing>`` invocation.
  cwdMissing?: boolean;
  // True while a /btw modal/capture is active for this PTY. During that
  // window the TUI can swallow subsequent bytes, so block all new submits
  // until the backend emits pty_submit_completed for the /btw attempt.
  btwCaptureActive?: boolean;
  defaultPermissionMode: PermissionMode;
  chatSendShortcut?: ChatSendShortcut;
  focusRequestKey?: number;
  // When a banner (e.g. take-over) sits directly above, flatten the card's top
  // so the two read as one connected unit (banner header + input body).
  flushTop?: boolean;
  // Placeholder shown when `disabled` — lets the parent explain *why* (fork vs
  // foreign-owned) instead of always claiming "fork/backup".
  disabledPlaceholder?: string;
}

export function ChatInput({
  sessionId,
  isStreaming,
  disabled,
  onStop,
  onSubmitStart,
  onSubmitFailed,
  cwdMissing = false,
  btwCaptureActive = false,
  defaultPermissionMode,
  chatSendShortcut = "enter",
  focusRequestKey = 0,
  flushTop = false,
  disabledPlaceholder,
}: ChatInputProps) {
  const [input, setInput] = useState("");
  const [stash, setStash] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [permissionMode, setPermissionMode] = useState<PermissionMode>(defaultPermissionMode);
  const [model, setModel] = useState<ModelId>("default");
  // Provider-aware composer copy (the composer only renders for drivable
  // providers; this keeps the prompt correct once Codex driving is enabled).
  const { provider } = useProviderTheme();
  // Reset the model selection to Auto when the provider changes — the option
  // sets differ (Claude vs Codex), so a stale claude-opus-* must not be passed
  // to `codex --model`. Fires only on an actual provider change.
  useEffect(() => {
    setModel("default");
  }, [provider]);

  const [autoRestoreSuppressed, setAutoRestoreSuppressed] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const wasStreamingRef = useRef(isStreaming);

  // In-flight AbortController for the on-focus PTY warm-up. Held at the
  // component level (not module) so unmounting cleans up; refocusing aborts
  // any prior in-flight request first. Because spawning is gated on textarea
  // focus (genuine intent to interact), the old "scroll the sidebar → 20
  // orphan PTYs" path no longer exists — browsing sessions spawns nothing.
  const warmupAbortRef = useRef<AbortController | null>(null);
  const focusedSessionRef = useRef<string | null>(null);

  const history = useMessageHistory(sessionId);

  useEffect(() => {
    setPermissionMode(defaultPermissionMode);
  }, [defaultPermissionMode]);

  useEffect(() => {
    if (focusRequestKey <= 0) return undefined;
    const raf = requestAnimationFrame(() => {
      textareaRef.current?.focus();
    });
    return () => cancelAnimationFrame(raf);
  }, [focusRequestKey]);

  // PTY kick-off is gated on *interaction intent*, not on reading. The
  // runner is lazy-spawn-on-focus; we trigger that spawn the moment the user
  // clicks into the textarea — the first clear signal they intend to send
  // rather than browse. The cold-boot of `claude` then overlaps the time the
  // user spends typing, so the first message still feels instant, while
  // rapidly switching between sessions to *read* spawns nothing at all.
  // pty/submit lazily re-focuses as the ultimate safety net (server.py),
  // so a missed or failed warm-up never blocks a send.
  //
  // Idle-already check: hit ptyStatus first; if a channel is live, skip the
  // spawn entirely. A mid-session model mismatch is handled by submit's
  // guarded switch_model path. The AbortController guards against focus →
  // blur → focus racing two requests.
  const handleTextareaFocus = useCallback(() => {
    if (!sessionId || disabled) return;
    focusedSessionRef.current = sessionId;
    const modelArg = model === "default" ? undefined : model;
    warmupAbortRef.current?.abort();
    const controller = new AbortController();
    warmupAbortRef.current = controller;
    void (async () => {
      try {
        const status = await api.ptyStatus(sessionId);
        if (controller.signal.aborted || status.alive) return;
        await api.ptyFocus(sessionId, modelArg);
      } catch {
        // Swallow — aborted (expected) or transient network error.
        // pty/submit lazily re-focuses, so a failed warm-up is recoverable.
      } finally {
        if (warmupAbortRef.current === controller) {
          warmupAbortRef.current = null;
        }
      }
    })();
  }, [sessionId, disabled, model]);

  // When the user navigates away from a session, abort any in-flight focus
  // warm-up and tell the BE to shorten the idle-kill window so a PTY we
  // spawned on intent doesn't linger for the full idle timeout. This is a
  // no-op for sessions that were only read (never focused → never spawned):
  // unfocus() early-returns when no channel exists.
  //
  // Keyed on sessionId + disabled ONLY (not model): a model change should
  // re-focus the same session, not blur-then-respawn it.
  useEffect(() => {
    if (!sessionId || disabled) return;
    return () => {
      warmupAbortRef.current?.abort();
      warmupAbortRef.current = null;
      if (focusedSessionRef.current === sessionId) {
        focusedSessionRef.current = null;
        void api.ptyBlur(sessionId).catch(() => {});
      }
    };
  }, [sessionId, disabled]);

  const autoSize = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = Math.min(el.scrollHeight, 200) + "px";
  }, []);

  // Combined input lifecycle effect:
  //   - resize the textarea to match its content (idempotent),
  //   - restore the stash when streaming finishes if the user hasn't typed.
  useEffect(() => {
    autoSize();
    const prev = wasStreamingRef.current;
    wasStreamingRef.current = isStreaming;
    if (prev && !isStreaming) {
      if (stash !== null && input.length === 0 && !autoRestoreSuppressed) {
        setInput(stash);
        setStash(null);
      }
    }
  }, [isStreaming, stash, input, autoRestoreSuppressed, autoSize]);

  const trimmed = input.trim();
  const isBtw = /^\/btw(?:\s|$)/i.test(trimmed);
  const isSlash = /^\/\S+/.test(trimmed);
  const submitKind: SubmitKind = isBtw ? "btw" : isSlash ? "slash" : "message";
  const canSend = !sending && !disabled && !btwCaptureActive && trimmed.length > 0 && (!isStreaming || isBtw);
  const sendShortcutLabel = chatSendShortcut === "modEnter" ? "Cmd/Ctrl+Enter" : "Enter";

  const handleSend = useCallback(async () => {
    // Surface the reason instead of silently no-op'ing so the user always
    // gets feedback when they hit Send.
    if (sending) { setError("Already sending — wait a moment."); return; }
    if (btwCaptureActive) {
      setError("A /btw response is still being captured. Wait for it to finish before sending another message.");
      return;
    }
    if (isStreaming && !isBtw) { setError("A turn is already in progress. Click Stop to cancel it."); return; }
    if (disabled) { setError("This session is a fork/backup — sending is disabled."); return; }
    if (cwdMissing) {
      setError("This session's working directory no longer exists. Messages can't be delivered — start a new chat from a valid directory.");
      return;
    }
    if (trimmed.length === 0) return;

    const text = trimmed;
    // Signal the parent BEFORE any network I/O so the optimistic
    // "Thinking" indicator flips on the same tick as the click.
    onSubmitStart?.({ kind: submitKind, content: text });
    // Stamp this session as agent-decoder-driven so the recap auto-trigger
    // can tell our turns apart from Claude Code CLI turns later.
    markClauDecodeSubmit(sessionId);
    // Append to history immediately (session detail catches up via SSE later)
    history.recordSend(text);
    setSending(true);
    setError(null);
    setInput("");
    setAutoRestoreSuppressed(false);
    // eslint-disable-next-line no-console
    console.log("[ChatInput] ptySubmit →", { sessionId, length: text.length });
    try {
      // PTY runner is the canonical send path (cost-blocker — see
      // docs/pty-runner-plan.md F1). ptySubmit lazy-focuses on the backend,
      // so this works even if the on-focus warm-up never ran. The TUI's
      // ack/stall verdict and JSONL streaming drive the indicator from
      // here on — no post-submit busy polling needed.
      await api.ptySubmit(sessionId, text, model === "default" ? undefined : model);
      // eslint-disable-next-line no-console
      console.log("[ChatInput] ptySubmit ✓");
    } catch (e) {
      // eslint-disable-next-line no-console
      console.error("[ChatInput] ptySubmit ✗", e);
      onSubmitFailed?.({ kind: submitKind, content: text });
      setInput((current) => (current.length === 0 ? text : current));
      setError(e instanceof Error ? e.message : "Failed to send message");
    } finally {
      setSending(false);
    }
  }, [sending, btwCaptureActive, isStreaming, isBtw, disabled, trimmed, sessionId, model, history, onSubmitStart, onSubmitFailed, cwdMissing, submitKind]);

  const handleStash = useCallback(() => {
    if (input.length === 0) return;
    setStash(input);
    setInput("");
    setAutoRestoreSuppressed(false);
  }, [input]);

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    const ta = e.currentTarget;

    // Up arrow — recall older message, but only when cursor is at position 0
    if (e.key === "ArrowUp" && !e.shiftKey && !e.ctrlKey && !e.metaKey
      && ta.selectionStart === 0 && ta.selectionEnd === 0) {
      const recalled = history.stepBack(input);
      if (recalled !== null) {
        setInput(recalled);
        // Keep cursor at position 0 so repeated Up presses keep navigating back
        requestAnimationFrame(() => {
          textareaRef.current?.setSelectionRange(0, 0);
        });
        e.preventDefault();
        return;
      }
      // If history was non-empty but we couldn't step further, still swallow the key.
      if (input.length === 0) {
        e.preventDefault();
        return;
      }
    }

    // Down arrow — recall newer message, but only when cursor is at end
    if (e.key === "ArrowDown" && !e.shiftKey && !e.ctrlKey && !e.metaKey
      && ta.selectionStart === input.length && ta.selectionEnd === input.length) {
      const recalled = history.stepForward();
      if (recalled !== null) {
        setInput(recalled);
        // Keep cursor at end so repeated Down presses keep navigating forward
        requestAnimationFrame(() => {
          textareaRef.current?.setSelectionRange(recalled.length, recalled.length);
        });
        e.preventDefault();
        return;
      }
    }

    if (
      chatSendShortcut === "enter"
      && e.key === "Enter"
      && !e.shiftKey
      && !e.metaKey
      && !e.ctrlKey
      && !e.altKey
    ) {
      e.preventDefault();
      handleSend();
      return;
    }
    if (
      chatSendShortcut === "modEnter"
      && e.key === "Enter"
      && (e.metaKey || e.ctrlKey)
      && !e.shiftKey
      && !e.altKey
    ) {
      e.preventDefault();
      handleSend();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "s") {
      e.preventDefault();
      handleStash();
      return;
    }
    if (e.ctrlKey && !e.metaKey) {
      const pos = ta.selectionStart;
      if (e.key === "e") {
        // Ctrl+E — move cursor to end of line
        e.preventDefault();
        const lineEnd = ta.value.indexOf("\n", pos);
        const target = lineEnd === -1 ? ta.value.length : lineEnd;
        ta.setSelectionRange(target, target);
        return;
      }
      if (e.key === "k") {
        // Ctrl+K — delete from cursor to end of line
        e.preventDefault();
        const lineEnd = ta.value.indexOf("\n", pos);
        const target = lineEnd === -1 ? ta.value.length : lineEnd;
        const next = ta.value.substring(0, pos) + ta.value.substring(target);
        setInput(next);
        // Restore cursor on next tick after React re-renders
        requestAnimationFrame(() => ta.setSelectionRange(pos, pos));
        return;
      }
    }
  };

  const handleInputChange = (e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const next = e.target.value;
    setInput(next);
    history.resetPosition();
    if (stash !== null && next.length > 0) {
      setAutoRestoreSuppressed(true);
    }
  };

  const handleStashBadgeClick = () => {
    if (stash === null) return;
    if (input.length === 0) {
      setInput(stash);
      setStash(null);
      setAutoRestoreSuppressed(false);
    } else {
      setInput(`${input}\n\n${stash}`);
      setStash(null);
      setAutoRestoreSuppressed(false);
    }
  };

  return (
    <div
      style={{
        flexShrink: 0,
        padding: flushTop ? "0 24px 18px" : "12px 24px 18px",
        background: "var(--bg-base)",
      }}
    >
      <div
        style={{
          maxWidth: "var(--message-max-width)",
          margin: "0 auto",
        }}
      >
        {error && <ErrorBanner message={error} />}

        {/* Chat input card. When a card sits above (flushTop, e.g. the take-over
            banner), stay fully rounded and rise UP over its bottom edge so the
            two read as two stacked pills (the input pill over the card behind). */}
        <div
          className="chat-input-card"
          style={{
            ...inputCardStyle,
            ...(flushTop
              ? {
                  position: "relative",
                  zIndex: 2,
                  marginTop: "-26px",
                  // Darker than the banner (--bg-input) so the lower input recedes
                  // and the banner reads as the emphasized foreground notice.
                  background: "var(--bg-tool-block)",
                }
              : {}),
            // When stacked over the take-over banner (flushTop), stay OPAQUE so the
            // card behind doesn't show through — the greyed placeholder already
            // signals the disabled state. The 0.6 dim is only for plain disabled
            // (e.g. is_fork) where there's no card behind.
            opacity: disabled && !flushTop ? 0.6 : 1,
          }}
        >
          <textarea
            ref={textareaRef}
            value={input}
            onChange={handleInputChange}
            onKeyDown={handleKeyDown}
            onFocus={handleTextareaFocus}
            disabled={disabled}
            rows={1}
            placeholder={
              disabled
                ? (disabledPlaceholder ?? "This session is a fork/backup — sending is disabled.")
                : provider === "codex"
                ? "Message Codex…"
                : "How can I help you today?"
            }
            style={textareaStyle}
          />

          {/* Bottom row inside the card */}
          <div style={bottomRowStyle}>
            {/* Left: stash slot (no attach button — we don't support attachments) */}
            <div style={{ display: "flex", alignItems: "center", gap: "8px", minHeight: "32px" }}>
              {stash !== null
                ? <StashBadge onClick={handleStashBadgeClick} />
                : <span aria-hidden="true" />}
            </div>

            {/* Right: unified settings pill + send/stop */}
            <div style={{ display: "flex", alignItems: "center", gap: "12px" }}>
              <ModelPicker
                model={model}
                setModel={setModel}
                permissionMode={permissionMode}
                setPermissionMode={setPermissionMode}
                defaultPermissionMode={defaultPermissionMode}
                provider={provider}
              />
              <SendShortcutHint label={sendShortcutLabel} />
              <SendStopButton
                isStreaming={isStreaming}
                sideChannelSendAvailable={isStreaming && isBtw && !btwCaptureActive}
                canSend={canSend}
                shortcutLabel={sendShortcutLabel}
                onSend={handleSend}
                onStop={onStop}
              />
            </div>
          </div>
        </div>

        <ChatDisclaimer />
      </div>
    </div>
  );
}

const inputCardStyle: React.CSSProperties = {
  background: "var(--bg-input)",
  border: "1px solid var(--border-default)",
  borderRadius: "18px",
  padding: "14px 18px 10px",
  boxShadow: "0 1px 2px rgba(0,0,0,0.04)",
};

const textareaStyle: React.CSSProperties = {
  width: "100%",
  // One line tall by default; autoSize() grows it to fit content up to 200px.
  minHeight: "26px",
  maxHeight: "200px",
  border: "none",
  outline: "none",
  resize: "none",
  background: "transparent",
  color: "var(--text-primary)",
  fontFamily: "var(--font-content)",
  fontSize: "15px",
  lineHeight: 1.6,
  padding: 0,
  overflowY: "auto",
  display: "block",
};

const bottomRowStyle: React.CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  gap: "10px",
  marginTop: "6px",
  paddingTop: "4px",
};

export default ChatInput;
