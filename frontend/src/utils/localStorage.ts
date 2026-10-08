// NOTE: storage keys keep the pre-rename "clau-decode:" prefix on purpose —
// renaming them would silently reset users' saved UI state.
export const LS = {
  STARRED: "clau-decode:starred",
  RENAMED: "clau-decode:renamed",
  ARCHIVED: "clau-decode:archived",
  VIEWED_AT: "clau-decode:viewed-at",
  FILE_VIEWER_WIDTH: "clau-decode:file-viewer-width",
  SIDEBAR_WIDTH: "clau-decode:sidebar-width",
  READ_SESSIONS_LEGACY: "clau-decode:read-sessions",
  // Map<sessionId, ms-since-epoch> — wall-clock of the most recent
  // submit through /api/pty/submit (agent-decoder → claude PTY). Used by
  // the recap auto-trigger to distinguish conversations whose last turn
  // was driven by agent-decoder from those driven by Claude Code CLI.
  LAST_SUBMIT_AT: "clau-decode:last-submit-at",
} as const;

export function lsGetSet(key: string): Set<string> {
  try { return new Set(JSON.parse(localStorage.getItem(key) ?? "[]")); }
  catch { return new Set(); }
}

export function lsPutSet(key: string, s: Set<string>): void {
  localStorage.setItem(key, JSON.stringify([...s]));
}

export function lsGetMap(key: string): Record<string, string> {
  try { return JSON.parse(localStorage.getItem(key) ?? "{}"); }
  catch { return {}; }
}

export function lsPutMap(key: string, m: Record<string, string>): void {
  localStorage.setItem(key, JSON.stringify(m));
}

export function lsGetRaw(key: string): string | null {
  return localStorage.getItem(key);
}

export function lsSetRaw(key: string, value: string): void {
  localStorage.setItem(key, value);
}

/**
 * Stamp the current wall-clock against ``sessionId`` in
 * ``LS.LAST_SUBMIT_AT``. Called inside the chat-input submit path so
 * that the recap auto-trigger can later check "was the last activity
 * on this session driven by agent-decoder?" by comparing this stamp
 * against ``SessionDetail.updated_at``.
 */
export function markAgentDecoderSubmit(sessionId: string): void {
  const map = lsGetMap(LS.LAST_SUBMIT_AT);
  map[sessionId] = String(Date.now());
  lsPutMap(LS.LAST_SUBMIT_AT, map);
}

/** Read the timestamp written by ``markAgentDecoderSubmit`` or null. */
export function readAgentDecoderSubmit(sessionId: string): number | null {
  const v = lsGetMap(LS.LAST_SUBMIT_AT)[sessionId];
  if (!v) return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

/**
 * One-time migration from the old read-sessions set to the new viewed-at map.
 *
 * The old format stored "sessionId:updatedAt" strings; we extract the latest
 * updatedAt per session so bells stay correctly dismissed after the upgrade.
 *
 * Idempotent: only runs if the legacy key exists and the new key has not yet
 * been written. Safe to call multiple times.
 *
 * Call once at app startup (from main.tsx, before createRoot) so the migration
 * doesn't fire as a module-load side effect under HMR or in unit tests.
 */
export function migrateReadSessions(): void {
  if (typeof window === "undefined") return;
  const raw = localStorage.getItem(LS.READ_SESSIONS_LEGACY);
  if (!raw) return;
  if (localStorage.getItem(LS.VIEWED_AT)) return;
  try {
    const entries: string[] = JSON.parse(raw);
    const map: Record<string, string> = {};
    for (const e of entries) {
      const colon = e.lastIndexOf(":");
      if (colon < 0) continue;
      const sid = e.slice(0, colon);
      const ts = e.slice(colon + 1);
      // Keep the most recent updatedAt per session
      if (!map[sid] || ts > map[sid]) map[sid] = ts;
    }
    localStorage.setItem(LS.VIEWED_AT, JSON.stringify(map));
  } catch { /* ignore */ }
}
