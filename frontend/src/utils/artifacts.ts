import type { Message } from "../api/types";
import { type FileCategory, getFileCategory } from "./fileType";

// Tools whose `file_path` / `notebook_path` input names a file the agent produced.
const WRITE_TOOLS = new Set(["Write", "Edit", "MultiEdit", "NotebookEdit"]);

const DOC_EXTS = new Set([".md", ".mdx", ".markdown", ".txt", ".rst", ".html", ".htm", ".csv", ".docx", ".rtf"]);

export type ArtifactGroup = "images" | "docs" | "media" | "code";

interface ArtifactBase {
  key: string;
  messageId: string;
  /** Index of the message it came from — newer is larger. */
  order: number;
  group: ArtifactGroup;
}

export type Artifact =
  | (ArtifactBase & { kind: "image"; src: string })
  | (ArtifactBase & { kind: "file"; path: string; category: FileCategory });

function imageSrc(source: unknown): string | null {
  const s = source as Record<string, string> | undefined;
  if (s?.type === "base64") return `data:${s.media_type};base64,${s.data}`;
  if (s?.type === "url") return s.url;
  return null;
}

function groupFor(path: string, category: FileCategory): ArtifactGroup {
  if (category === "image") return "images";
  if (category === "pdf") return "docs";
  if (category === "audio" || category === "video") return "media";
  const dot = path.lastIndexOf(".");
  return dot !== -1 && DOC_EXTS.has(path.slice(dot).toLowerCase()) ? "docs" : "code";
}

/**
 * Collect a session's artifacts, newest first: images attached to messages or
 * returned by tools, plus files the agent wrote or edited (one entry per path,
 * placed at its most recent touch).
 */
export function extractArtifacts(messages: Message[]): Artifact[] {
  const out: Artifact[] = [];
  const files = new Map<string, Artifact>();
  messages.forEach((m, order) => {
    const base = { messageId: m.id, order };
    m.content_blocks.forEach((b, bi) => {
      if (b.type === "image") {
        const src = imageSrc(b.source);
        if (src) out.push({ ...base, kind: "image", key: `${m.id}:${bi}`, group: "images", src });
      } else if (b.type === "tool_result" && Array.isArray(b.content)) {
        b.content.forEach((c, ci) => {
          const src = c.type === "image" ? imageSrc((c as { source?: unknown }).source) : null;
          if (src) out.push({ ...base, kind: "image", key: `${m.id}:${bi}:${ci}`, group: "images", src });
        });
      } else if (b.type === "tool_use" && WRITE_TOOLS.has(b.name)) {
        const path = b.input.file_path ?? b.input.notebook_path;
        if (typeof path !== "string" || !path) return;
        const category = getFileCategory(path);
        files.set(path, { ...base, kind: "file", key: path, group: groupFor(path, category), path, category });
      }
    });
  });
  return [...out, ...files.values()].sort((a, b) => b.order - a.order);
}
