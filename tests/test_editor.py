"""Tests for editor.py — backup and surgical JSONL write."""

import json
import shutil
from pathlib import Path


def _write_session_file(path: Path, lines: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )


def _make_record(
    uuid: str, role: str, text: str, parent_uuid: str | None = None
) -> dict:
    return {
        "type": role,
        "uuid": uuid,
        "parentUuid": parent_uuid,
        "timestamp": "2026-05-05T10:00:00.000Z",
        "isSidechain": False,
        "isMeta": False,
        "message": {
            "role": role,
            "content": [{"type": "text", "text": text}],
        },
    }


class TestBackupSession:
    def test_creates_backup_file(self, tmp_path):
        from agent_decoder.editor import backup_session

        src = tmp_path / "session.jsonl"
        src.write_text('{"type":"user"}\n', encoding="utf-8")
        backup = backup_session(src)
        assert backup.exists()
        assert backup.read_text() == src.read_text()

    def test_backup_name_contains_bak_and_jsonl_suffix(self, tmp_path):
        from agent_decoder.editor import backup_session

        src = tmp_path / "session.jsonl"
        src.write_text("{}\n", encoding="utf-8")
        backup = backup_session(src)
        assert ".bak." in backup.name
        assert backup.suffix == ".jsonl"

    def test_original_unchanged_after_backup(self, tmp_path):
        from agent_decoder.editor import backup_session

        src = tmp_path / "session.jsonl"
        original = '{"type":"user","uuid":"abc"}\n'
        src.write_text(original, encoding="utf-8")
        backup_session(src)
        assert src.read_text() == original


class TestDeleteFromSession:
    def test_deletes_matching_uuid(self, tmp_path):
        from agent_decoder.editor import delete_from_session

        src = tmp_path / "s.jsonl"
        lines = [
            _make_record("aaa", "user", "hello"),
            _make_record("bbb", "assistant", "world"),
        ]
        _write_session_file(src, lines)
        delete_from_session(src, "aaa")
        remaining = [
            json.loads(line) for line in src.read_text().splitlines() if line.strip()
        ]
        assert len(remaining) == 1
        assert remaining[0]["uuid"] == "bbb"

    def test_preserves_non_message_records(self, tmp_path):
        from agent_decoder.editor import delete_from_session

        src = tmp_path / "s.jsonl"
        meta = {"type": "custom-title", "customTitle": "My Session"}
        msg = _make_record("aaa", "user", "hello")
        _write_session_file(src, [meta, msg])
        delete_from_session(src, "aaa")
        remaining = [
            json.loads(line) for line in src.read_text().splitlines() if line.strip()
        ]
        assert len(remaining) == 1
        assert remaining[0]["type"] == "custom-title"

    def test_noop_when_uuid_not_found(self, tmp_path):
        from agent_decoder.editor import delete_from_session

        src = tmp_path / "s.jsonl"
        lines = [_make_record("aaa", "user", "hello")]
        _write_session_file(src, lines)
        original = src.read_text()
        delete_from_session(src, "nonexistent")
        assert src.read_text() == original


class TestEditContentInSession:
    def test_replaces_content_in_matching_line(self, tmp_path):
        from agent_decoder.editor import edit_content_in_session

        src = tmp_path / "s.jsonl"
        lines = [_make_record("aaa", "user", "original text")]
        _write_session_file(src, lines)
        new_blocks = [{"type": "text", "text": "updated text"}]
        edit_content_in_session(src, "aaa", new_blocks)
        updated = json.loads(src.read_text().splitlines()[0])
        assert updated["message"]["content"] == new_blocks

    def test_preserves_all_other_fields_in_line(self, tmp_path):
        from agent_decoder.editor import edit_content_in_session

        src = tmp_path / "s.jsonl"
        lines = [_make_record("aaa", "user", "hello")]
        _write_session_file(src, lines)
        edit_content_in_session(src, "aaa", [{"type": "text", "text": "new"}])
        updated = json.loads(src.read_text().splitlines()[0])
        assert updated["uuid"] == "aaa"
        assert updated["timestamp"] == "2026-05-05T10:00:00.000Z"
        assert updated["type"] == "user"

    def test_noop_when_uuid_not_found(self, tmp_path):
        from agent_decoder.editor import edit_content_in_session

        src = tmp_path / "s.jsonl"
        lines = [_make_record("aaa", "user", "hello")]
        _write_session_file(src, lines)
        original = src.read_text()
        edit_content_in_session(src, "zzz", [{"type": "text", "text": "new"}])
        assert src.read_text() == original


# ---------------------------------------------------------------------------
# Step 3: MANDATORY GATE — round-trip tests
# ---------------------------------------------------------------------------

_SESSION_UUID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
_MSG_UUID_1 = "11111111-0000-0000-0000-000000000001"
_MSG_UUID_2 = "11111111-0000-0000-0000-000000000002"


class TestRoundTrip:
    """Prove parse → delete/edit → re-parse gives consistent results."""

    def test_delete_roundtrip(self, tmp_path):
        from agent_decoder.editor import delete_from_session
        from agent_decoder.parser import parse_session

        src = tmp_path / f"{_SESSION_UUID}.jsonl"
        _write_session_file(
            src,
            [
                _make_record(_MSG_UUID_1, "user", "hello"),
                _make_record(_MSG_UUID_2, "assistant", "world"),
            ],
        )
        delete_from_session(src, _MSG_UUID_1)
        _, messages = parse_session(src)
        assert len(messages) == 1
        assert messages[0].id == _MSG_UUID_2

    def test_edit_roundtrip(self, tmp_path):
        from agent_decoder.editor import edit_content_in_session
        from agent_decoder.models import TextBlock
        from agent_decoder.parser import parse_session

        src = tmp_path / f"{_SESSION_UUID}.jsonl"
        _write_session_file(src, [_make_record(_MSG_UUID_1, "user", "original")])
        edit_content_in_session(src, _MSG_UUID_1, [{"type": "text", "text": "updated"}])
        _, messages = parse_session(src)
        assert len(messages) == 1
        assert isinstance(messages[0].content_blocks[0], TextBlock)
        assert messages[0].content_blocks[0].text == "updated"

    def test_non_message_records_survive_delete(self, tmp_path):
        from agent_decoder.editor import delete_from_session
        from agent_decoder.parser import parse_session

        src = tmp_path / f"{_SESSION_UUID}.jsonl"
        _write_session_file(
            src,
            [
                {"type": "custom-title", "customTitle": "Keep Me"},
                _make_record(_MSG_UUID_1, "user", "delete me"),
            ],
        )
        delete_from_session(src, _MSG_UUID_1)
        session, messages = parse_session(src)
        assert len(messages) == 0
        assert session.title == "Keep Me"

    def test_swap_session_bumps_mtime_on_content_edit(self, tmp_path):
        """swap_session must advance the file's mtime even when the edit
        doesn't change message timestamps — content edits leave timestamps
        intact, so the parser-derived updated_at would otherwise stay flat
        and downstream dedupe (which keys on detail.updated_at / file mtime)
        would miss the mutation. Regression test for the fix that adds an
        explicit os.utime() bump at the end of swap_session.
        """
        import os
        import time

        from agent_decoder.editor import swap_session

        src = tmp_path / f"{_SESSION_UUID}.jsonl"
        _write_session_file(src, [_make_record(_MSG_UUID_1, "user", "original")])

        # Force a baseline mtime in the past so even coarse-resolution
        # filesystems can demonstrate advance.
        past = time.time() - 60
        os.utime(src, (past, past))
        before = src.stat().st_mtime
        assert before == past  # sanity

        swap_session(
            src,
            _SESSION_UUID,
            edit_uuid=_MSG_UUID_1,
            new_content=[{"type": "text", "text": "updated"}],
        )

        after = src.stat().st_mtime
        assert after > before, (
            f"swap_session must bump mtime: before={before}, after={after}"
        )

    def test_swap_session_bumps_mtime_on_delete(self, tmp_path):
        """Same guarantee on delete — even when the file content shrinks
        and the write happens in the same wall-clock second as the
        baseline, mtime must advance.
        """
        import os
        import time

        from agent_decoder.editor import swap_session

        src = tmp_path / f"{_SESSION_UUID}.jsonl"
        _write_session_file(
            src,
            [
                _make_record(_MSG_UUID_1, "user", "keep me"),
                _make_record(_MSG_UUID_2, "assistant", "delete me"),
            ],
        )
        past = time.time() - 60
        os.utime(src, (past, past))
        before = src.stat().st_mtime

        swap_session(src, _SESSION_UUID, delete_uuid=_MSG_UUID_2)

        after = src.stat().st_mtime
        assert after > before, (
            f"swap_session must bump mtime on delete: before={before}, after={after}"
        )

    def test_backup_restore_roundtrip(self, tmp_path):
        from agent_decoder.editor import backup_session, delete_from_session
        from agent_decoder.parser import parse_session

        src = tmp_path / f"{_SESSION_UUID}.jsonl"
        _write_session_file(
            src,
            [
                _make_record(_MSG_UUID_1, "user", "keep me"),
                _make_record(_MSG_UUID_2, "assistant", "delete me"),
            ],
        )
        backup = backup_session(src)
        delete_from_session(src, _MSG_UUID_2)
        # Restore from backup
        shutil.copy2(backup, src)
        _, messages = parse_session(src)
        assert len(messages) == 2
        assert {m.id for m in messages} == {_MSG_UUID_1, _MSG_UUID_2}
