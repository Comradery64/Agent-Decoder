import { useState } from "react";
import { api } from "../../api/client";
import type { PtyOwnership } from "../../api/types";
import {
  chatCardButtonStyle,
  chatCardColumnStyle,
  chatCardOuterStyle,
  chatCardStyle,
} from "./chatCard";

// Phase-0 take-over banner (pty-ownership-plan.md). Renders above
// ChatInputBar when /api/pty/ownership reports another claude is
// attached to this session. Clicking Take over SIGINTs that claude on
// the BE and refetches ownership; on success the banner unmounts.
//
// The banner is the *only* affordance for the take-over action — the
// plan deliberately doesn't offer "submit anyway" so the user never
// races the foreign claude blindly.
export function OwnershipBanner({
  sessionId,
  ownership,
  onTookOver,
}: {
  sessionId: string;
  ownership: PtyOwnership | null;
  onTookOver: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleTakeover = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.ptyTakeover(sessionId);
      // Take-over only releases the foreign pid; we now need to attach
      // here so the badge flips 🟡 → 🟢 (not 🟡 → ⚪️). Failure of this
      // step is non-fatal — the user's next chat-input focus will retry.
      await api.ptyFocus(sessionId).catch(() => {});
      onTookOver();
    } catch (e) {
      // Surface a terse error; the body shape (timeout vs permission)
      // is handled uniformly. Common cases:
      //   409 — claude didn't release within 3 s; user can retry.
      //   403 — cross-user pid; user has to act manually.
      const msg = e instanceof Error ? e.message : String(e);
      setError(
        /403/.test(msg)
          ? "Can't take over — the other claude is owned by a different user."
          : /409/.test(msg)
          ? "Take-over timed out (3 s). The other claude is still busy — try again."
          : "Take-over failed. Check the server log."
      );
    } finally {
      setBusy(false);
    }
  };

  // Phase-1 metadata wins over the bare pid list when a sidecar is
  // present. ``kind`` tells the user whether it's a peer agent-decoder
  // (with a clickable ui_endpoint) or a wrapped terminal claude.
  const fo = ownership?.foreign_owner;
  const heading = fo
    ? `This session is open in ${fo.kind} @ ${fo.hostname} (pid ${fo.pid}).`
    : ownership && ownership.foreign_pids.length > 0
      ? `This session is open in another claude (pid ${ownership.foreign_pids.join(", ")}).`
      : "This session is open in another claude.";

  return (
    // Two stacked pill-cards: this banner is a fully-rounded card that sits
    // BEHIND, and the composer below rises up over its bottom edge (flushTop adds
    // a negative margin + higher z-index). paddingBottom:0 keeps the overlap tight.
    <div role="alert" style={{ ...chatCardOuterStyle, paddingBottom: 0 }}>
      <div style={chatCardColumnStyle}>
        <div
          style={{
            // The "popup" card sits BEHIND; the composer below rises up in front
            // of it. Its bottom corners are SQUARE (and tall, extending well
            // behind the composer) so no rounded corner can peek through the gaps
            // left by the composer's own rounded top corners. Generous bottom
            // padding so the deep overlap never clips the subtitle.
            ...chatCardStyle,
            position: "relative",
            zIndex: 1,
            marginTop: "-8px",
            paddingBottom: "30px",
            borderBottomLeftRadius: 0,
            borderBottomRightRadius: 0,
          }}
        >
          {/* Single row: title + subtitle stacked on the left, Take over button
              on the right, vertically centered across both lines. */}
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "20px" }}>
            <div style={{ minWidth: 0, fontFamily: "var(--font-content)", lineHeight: 1.45 }}>
              <div style={{ fontSize: "14px", fontWeight: 600, color: "var(--text-primary)" }}>
                {heading}
              </div>
              <div style={{ fontSize: "14px", color: "var(--text-secondary)" }}>
                Sending here would race the other process. Take over to attach here instead.
                {fo?.ui_endpoint && (
                  <> Or open the other instance:{" "}
                    <a
                      href={fo.ui_endpoint}
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: "var(--text-primary)", textDecoration: "underline" }}
                    >
                      {fo.ui_endpoint}
                    </a>.
                  </>
                )}
              </div>
              {error && (
                <div style={{ marginTop: "6px", color: "var(--accent-red, #c47a7a)", fontSize: "13px" }}>
                  {error}
                </div>
              )}
            </div>
            <button
              type="button"
              onClick={handleTakeover}
              disabled={busy}
              style={{ ...chatCardButtonStyle, flexShrink: 0, cursor: busy ? "wait" : "pointer", opacity: busy ? 0.7 : 1 }}
            >
              {busy ? "Taking over…" : "Take over"}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
