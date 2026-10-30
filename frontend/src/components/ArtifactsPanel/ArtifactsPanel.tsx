import { useMemo, useState } from "react";
import { useAppStore } from "../../store";
import { useSessionDetail } from "../Messages/hooks/useSessionDetail";
import { ScrollContainer } from "../ScrollContainer";
import { type Artifact, type ArtifactGroup, extractArtifacts } from "../../utils/artifacts";
import { blobUrl } from "../../utils/fileType";

const WIDTH = 320;

const FILTERS: Array<{ id: ArtifactGroup | "all"; label: string }> = [
  { id: "all", label: "All" },
  { id: "images", label: "Images" },
  { id: "docs", label: "Docs" },
  { id: "media", label: "Media" },
  { id: "code", label: "Code" },
];

function basename(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1) || path;
}

function IconFile() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}

function ArtifactCard({ artifact }: { artifact: Artifact }) {
  const setViewingFilePath = useAppStore((s) => s.setViewingFilePath);
  const setPendingScrollMessageId = useAppStore((s) => s.setPendingScrollMessageId);
  const jump = () => setPendingScrollMessageId(artifact.messageId);
  // File artifacts open in the file viewer; message images jump to their turn.
  const open = artifact.kind === "file" ? () => setViewingFilePath(artifact.path) : jump;
  const thumb =
    artifact.kind === "image" ? artifact.src
    : artifact.category === "image" ? blobUrl(artifact.path)
    : null;
  const label = artifact.kind === "file" ? basename(artifact.path) : "Image";

  return (
    <div
      style={{
        border: "1px solid var(--border-subtle)",
        borderRadius: "var(--radius-md)",
        overflow: "hidden",
        background: "var(--bg-tool-block)",
      }}
    >
      <button
        type="button"
        onClick={open}
        title={artifact.kind === "file" ? artifact.path : "Jump to message"}
        style={{
          display: "flex",
          alignItems: "center",
          gap: "8px",
          width: "100%",
          padding: thumb ? 0 : "10px 12px",
          background: "none",
          border: "none",
          cursor: "pointer",
          color: "var(--text-secondary)",
          textAlign: "left",
        }}
      >
        {thumb ? (
          <img
            src={thumb}
            alt={label}
            loading="lazy"
            style={{ display: "block", width: "100%", maxHeight: 360, objectFit: "contain", background: "var(--bg-base)" }}
          />
        ) : (
          <>
            <IconFile />
            <span style={{ fontFamily: "var(--font-mono)", fontSize: "12px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {label}
            </span>
          </>
        )}
      </button>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: "8px",
          padding: "4px 8px 4px 12px",
          borderTop: "1px solid var(--border-subtle)",
          fontSize: "11px",
          color: "var(--text-tertiary)",
        }}
      >
        <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
          {thumb ? label : artifact.group}
        </span>
        <button
          type="button"
          onClick={jump}
          title="Jump to message"
          style={{ background: "none", border: "none", cursor: "pointer", color: "inherit", fontSize: "11px", padding: "2px 4px", flexShrink: 0 }}
        >
          Show in chat ↗
        </button>
      </div>
    </div>
  );
}

export default function ArtifactsPanel() {
  const sessionId = useAppStore((s) => s.selectedSessionId);
  // Re-key per session so the filter resets when switching chats.
  return sessionId ? <ArtifactsList key={sessionId} sessionId={sessionId} /> : null;
}

function ArtifactsList({ sessionId }: { sessionId: string }) {
  const { detail } = useSessionDetail(sessionId);
  const toggleArtifacts = useAppStore((s) => s.toggleArtifacts);
  const [filter, setFilter] = useState<ArtifactGroup | "all">("all");
  const artifacts = useMemo(() => extractArtifacts(detail?.messages ?? []), [detail?.messages]);
  const present = new Set(artifacts.map((a) => a.group));
  const shown = filter === "all" ? artifacts : artifacts.filter((a) => a.group === filter);

  return (
    <aside
      aria-label="Artifacts"
      style={{
        width: WIDTH,
        flexShrink: 0,
        display: "flex",
        flexDirection: "column",
        borderLeft: "1px solid var(--border-subtle)",
        background: "var(--bg-base)",
        minHeight: 0,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", padding: "12px 12px 8px 16px" }}>
        <span style={{ fontSize: "13px", fontWeight: 600, color: "var(--text-primary)" }}>
          Artifacts <span style={{ color: "var(--text-tertiary)", fontWeight: 400 }}>{artifacts.length}</span>
        </span>
        <button
          type="button"
          onClick={toggleArtifacts}
          aria-label="Close artifacts"
          title="Close artifacts"
          style={{ background: "none", border: "none", cursor: "pointer", color: "var(--text-tertiary)", fontSize: "16px", padding: "0 4px" }}
        >
          ×
        </button>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "4px", padding: "0 12px 8px 16px" }}>
        {FILTERS.filter((f) => f.id === "all" || present.has(f.id)).map((f) => (
          <button
            key={f.id}
            type="button"
            onClick={() => setFilter(f.id)}
            aria-pressed={filter === f.id}
            style={{
              fontSize: "11px",
              padding: "3px 8px",
              borderRadius: "999px",
              border: "1px solid var(--border-subtle)",
              cursor: "pointer",
              background: filter === f.id ? "var(--bg-sidebar-hover)" : "none",
              color: filter === f.id ? "var(--text-primary)" : "var(--text-tertiary)",
            }}
          >
            {f.label}
          </button>
        ))}
      </div>
      <ScrollContainer style={{ flex: 1, minHeight: 0 }}>
        <div style={{ display: "flex", flexDirection: "column", gap: "10px", padding: "4px 12px 16px 16px" }}>
          {shown.length === 0 ? (
            <div style={{ fontSize: "12px", color: "var(--text-tertiary)", padding: "24px 0", textAlign: "center" }}>
              {detail ? "No artifacts in this session yet." : "Loading…"}
            </div>
          ) : (
            shown.map((a) => <ArtifactCard key={a.key} artifact={a} />)
          )}
        </div>
      </ScrollContainer>
    </aside>
  );
}
