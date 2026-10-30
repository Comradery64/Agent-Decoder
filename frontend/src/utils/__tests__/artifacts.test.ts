import { describe, expect, it } from "vitest";
import type { ContentBlock, Message } from "../../api/types";
import { extractArtifacts } from "../artifacts";

function msg(id: string, content_blocks: ContentBlock[]): Message {
  return {
    id, session_id: "s", parent_id: null, role: "assistant", content_blocks,
    timestamp: null, model: null, is_sidechain: false, is_meta: false, cwd: null,
    git_branch: null, source_tool_assistant_uuid: null, usage: null,
  };
}

const write = (id: string, file_path: string): ContentBlock =>
  ({ type: "tool_use", id, name: "Write", input: { file_path } });

describe("extractArtifacts", () => {
  it("collects images and written files, newest first, one entry per path", () => {
    const out = extractArtifacts([
      msg("m1", [{ type: "image", source: { type: "base64", media_type: "image/png", data: "AAA" } }]),
      msg("m2", [write("t1", "/p/README.md"), write("t2", "/p/app.ts")]),
      msg("m3", [{
        type: "tool_result", tool_use_id: "t9", is_error: false,
        content: [{ type: "image", source: { type: "url", url: "http://x/shot.png" } } as { type: string }],
      }]),
      msg("m4", [{ type: "tool_use", id: "t3", name: "Edit", input: { file_path: "/p/README.md" } }]),
      msg("m5", [{ type: "tool_use", id: "t4", name: "Read", input: { file_path: "/p/other.pdf" } }]),
    ]);
    expect(out.map((a) => [a.messageId, a.group, a.kind === "file" ? a.path : a.src])).toEqual([
      ["m4", "docs", "/p/README.md"],
      ["m3", "images", "http://x/shot.png"],
      ["m2", "code", "/p/app.ts"],
      ["m1", "images", "data:image/png;base64,AAA"],
    ]);
  });
});
