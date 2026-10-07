type AppEventMap = {
  "config-updated": import("../api/types").AppConfig;
  "refresh": void;
  "rename": { id: string; title: string };
  "star": string;          // sessionId
  "archive": string;       // sessionId
  "session-mutated": string; // sessionId
  "sessions-removed": string[]; // sessionIds removed optimistically (delete) — drop from lists immediately, server reconciles via "refresh"
  // PTY input watchdog signals — drive the "Thinking" indicator's
  // hide/error states without blind timers.
  "pty-input-acknowledged": { session_id: string };
  "pty-input-stalled": { session_id: string; elapsed_ms: number };
  "pty-submit-completed": {
    session_id: string;
    kind: "btw" | "slash" | "message" | string;
    status: "completed" | "acknowledged" | "failed" | "timed_out" | string;
    input_id?: number | null;
    response_id?: number | null;
  };
  "pty-output-chunk": {
    session_id: string;
    data_b64: string;
  };
  "pty-native-state": {
    session_id: string;
    state: string;
    decoded_input_safe: boolean;
  };
  // Phase 2: a /btw input row was persisted. The response may still be
  // pending, so views should refetch ephemerals and show Capturing response…
  "ephemeral-input-persisted": {
    session_id: string;
    input_id: number;
    kind: string;
  };
  // Phase 2: a /btw exchange fully captured — refetch ephemerals for the session.
  "ephemeral-pair-persisted": {
    session_id: string;
    input_id: number;
    response_id: number;
    kind: string;
  };
  // Transient user-facing notification (e.g. a failed fire-and-forget action).
  "toast": { message: string; kind?: "error" | "info" };
};

const PFX = "agent-decoder:";

export function emit<K extends keyof AppEventMap>(name: K, detail: AppEventMap[K]): void {
  window.dispatchEvent(new CustomEvent(`${PFX}${name}`, { detail }));
}

export function on<K extends keyof AppEventMap>(
  name: K,
  handler: (detail: AppEventMap[K]) => void,
): () => void {
  const wrapped = (e: Event) => handler((e as CustomEvent<AppEventMap[K]>).detail);
  window.addEventListener(`${PFX}${name}`, wrapped);
  return () => window.removeEventListener(`${PFX}${name}`, wrapped);
}
