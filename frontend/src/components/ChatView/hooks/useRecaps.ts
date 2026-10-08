import { useCallback, useEffect, useState } from "react";
import type { AppConfig, Recap } from "../../../api/types";
import { api } from "../../../api/client";
import { readAgentDecoderSubmit } from "../../../utils/localStorage";

// How far apart the session's latest activity (assistant or user message
// timestamp) and our last agent-decoder submit can be before we conclude
// "Claude Code CLI drove the last turn, not us." 30 minutes comfortably
// covers slow backends (zai up to ~7 min observed) and chained tool
// calls. If CLI activity happens within this window of our submit, we'll
// false-positive — but in practice you're either using agent-decoder or
// you're not, so the overlap case is rare. Upgrade to per-message
// provenance if it becomes a real problem.
const OURS_WINDOW_MS = 30 * 60_000;

// Minimum conversation depth to be considered "a real active session".
// Below this we suppress the auto-recap — tiny test sessions don't need
// catch-up summaries. 4 messages ≈ 2 user turns + 2 assistant replies.
const MIN_MESSAGE_COUNT_FOR_RECAP = 4;

// Hover-debounce. Unlike Claude Code (where opening a tab is a clear
// "I'm here now" signal), agent-decoder users navigate sidebar entries
// in transit — clicking through to find the right session. Don't burn
// a recap-generation call until they've actually settled on a session.
const RECAP_DWELL_MS = 3000;

// Recap orchestration for a single session.
//
// "Idle" semantics — the auto-gen trigger:
//   The clock runs from the *last conversation activity* (i.e. the
//   newest message timestamp on the session, exposed via the API as
//   ``SessionDetail.updated_at``), NOT from the last time the user
//   opened the sidebar entry. Translating the user-facing intent: a
//   recap appears when you come back to a chat whose last assistant
//   reply landed >N minutes ago — N = ``claude_recap_idle_minutes``.
//
// "Once per app session" — the noisiness guard:
//   ``recapShownInSession`` is a module-level Set tracking which
//   session ids auto-fired in this tab's lifetime. We add to it the
//   instant we kick off an auto-gen and never auto-fire for the same
//   session again until the page reloads. Without this, navigating
//   away from a session and back within the same app session would
//   re-fire the recap as long as the conversation stayed idle.
//   Manual ``regenerate(...)`` ignores the guard — the user knows
//   what they want.
//
// Returns:
//   recaps — current list to render
//   recapGenerating — true while a generation request is in flight
//   regenerate / dismiss — actions to wire into MessageList
export interface RecapState {
  recaps: Recap[];
  recapGenerating: boolean;
  recapPromptPending: boolean;
  generateRecap(sessionId: string, replaceId?: number): Promise<void>;
  dismissRecapPrompt(): void;
  dismiss(sessionId: string, recapId: number): void;
}

const recapShownInSession = new Set<string>();

function idleMinutesSince(iso: string | null | undefined): number | null {
  if (!iso) return null;
  const ms = Date.parse(iso);
  if (!Number.isFinite(ms)) return null;
  return (Date.now() - ms) / 60000;
}

export function useRecaps(
  selectedSessionId: string | null,
  appConfig: AppConfig | null,
  messageCount: number,
  // ISO timestamp of the session's last activity (newest message),
  // i.e. ``SessionDetail.updated_at``. Null while detail is loading
  // or for sessions with no messages — in either case auto-gen skips.
  lastActivityAt: string | null,
  // When false, the session's cwd is gone and the BE will 404 every
  // recap-generation request. Skip the auto-gen path entirely.
  cwdExists: boolean = true,
): RecapState {
  const [recaps, setRecaps] = useState<Recap[]>([]);
  const [recapGenerating, setRecapGenerating] = useState<boolean>(false);
  const [recapPromptPending, setRecapPromptPending] = useState<boolean>(false);

  const generateRecap = useCallback(async (sessionId: string, replaceId?: number) => {
    if (replaceId !== undefined) {
      api.dismissRecap(sessionId, replaceId).catch(() => {});
      setRecaps((prev) => prev.filter((r) => r.id !== replaceId));
    }
    setRecapPromptPending(false);
    setRecapGenerating(true);
    try {
      const r = await api.generateRecap(sessionId);
      setRecaps((prev) => (prev.some((x) => x.id === r.id) ? prev : [...prev, r]));
    } catch {
      // Keep recap as a non-blocking affordance. Failures are silent for now,
      // matching the old auto-recap behavior.
    } finally {
      setRecapGenerating(false);
    }
  }, []);

  const dismissRecapPrompt = useCallback(() => {
    setRecapPromptPending(false);
  }, []);

  const dismiss = useCallback((sessionId: string, recapId: number) => {
    setRecaps((prev) => prev.filter((r) => r.id !== recapId));
    api.dismissRecap(sessionId, recapId).catch(() => {});
  }, []);

  useEffect(() => {
    // Reset on session change BEFORE any async work, so the previous
    // session's recap card (or its "Generating…" spinner) can't flash
    // through during the gap between this effect running and the
    // listRecaps response landing. Without this, navigating A→B briefly
    // shows A's recap inside B's view.
    setRecaps([]);
    setRecapGenerating(false);
    setRecapPromptPending(false);
    if (!selectedSessionId) return;
    let cancelled = false;
    const sid = selectedSessionId;
    const idleMin = idleMinutesSince(lastActivityAt);

    api.listRecaps(sid).then((rs) => {
      if (!cancelled) setRecaps(rs);
    }).catch(() => {});

    // "Ours-vs-CLI" gate: only auto-recap when the session's latest
    // activity falls within ``OURS_WINDOW_MS`` of our last agent-decoder
    // submit. If the session has been driven by Claude Code CLI more
    // recently than us, the gap blows past the window and we stay quiet.
    const lastSubmitMs = readAgentDecoderSubmit(sid);
    const lastActivityMs = lastActivityAt ? Date.parse(lastActivityAt) : NaN;
    const lastTurnWasOurs = (
      lastSubmitMs != null
      && Number.isFinite(lastActivityMs)
      && lastActivityMs - lastSubmitMs >= 0           // our submit didn't post-date the latest msg
      && lastActivityMs - lastSubmitMs <= OURS_WINDOW_MS
    );

    const eligible = (
      appConfig?.claude_recap_enabled &&
      idleMin != null &&
      idleMin >= appConfig.claude_recap_idle_minutes &&
      messageCount >= MIN_MESSAGE_COUNT_FOR_RECAP &&
      cwdExists &&
      lastTurnWasOurs &&
      !recapShownInSession.has(sid)
    );

    let dwellTimer: ReturnType<typeof setTimeout> | null = null;
    if (eligible) {
      dwellTimer = setTimeout(() => {
        if (cancelled) return;
        recapShownInSession.add(sid);
        setRecapPromptPending(true);
      }, RECAP_DWELL_MS);
    }

    return () => {
      cancelled = true;
      if (dwellTimer) clearTimeout(dwellTimer);
    };
  }, [selectedSessionId, appConfig, messageCount, lastActivityAt, cwdExists]);

  return {
    recaps,
    recapGenerating,
    recapPromptPending,
    generateRecap,
    dismissRecapPrompt,
    dismiss,
  };
}
