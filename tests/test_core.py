"""Core tests for Claude Chronicle — covering known failure modes."""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ccfolio.autotitle import generate_auto_title, _prompt_topics
from ccfolio.markdown import generate_filename
from ccfolio.models import Session
from ccfolio.parser import parse_session_file, _clean_first_prompt


# ── Helpers ──────────────────────────────────────────────────────────

def _make_session(**kwargs) -> Session:
    """Create a minimal Session with sensible defaults."""
    s = Session(session_id="test-session-id")
    for k, v in kwargs.items():
        setattr(s, k, v)
    return s


def _write_jsonl(tmp_path: Path, lines: list[dict]) -> Path:
    """Write a list of dicts as JSONL to a temp file."""
    f = tmp_path / "test-session.jsonl"
    f.write_text("\n".join(json.dumps(line) for line in lines), encoding="utf-8")
    return f


# ── generate_filename ─────────────────────────────────────────────────

class TestGenerateFilename:
    def test_with_created_at(self):
        s = _make_session(
            created_at=datetime(2026, 2, 27, 14, 30, tzinfo=timezone.utc),
            custom_title="Test Session",
        )
        name = generate_filename(s)
        assert name == "202602271430 - Test Session.md"

    def test_fallback_to_modified_at(self):
        """When created_at is None, use modified_at."""
        s = _make_session(
            created_at=None,
            modified_at=datetime(2026, 1, 15, 9, 0, tzinfo=timezone.utc),
            custom_title="My Session",
        )
        name = generate_filename(s)
        assert name.startswith("202601150900")
        assert " - " in name
        assert not name.startswith(" - ")

    def test_fallback_to_source_mtime(self):
        """When both timestamps are None, use source_mtime."""
        s = _make_session(
            created_at=None,
            modified_at=None,
            source_mtime=datetime(2026, 3, 1, 12, 0, tzinfo=timezone.utc).timestamp(),
            custom_title="File Date Session",
        )
        name = generate_filename(s)
        assert " - " in name
        assert not name.startswith(" - ")
        assert not name.startswith("undated")  # source_mtime available

    def test_fallback_to_undated(self):
        """When all timestamps are missing, use 'undated' prefix."""
        s = _make_session(
            created_at=None,
            modified_at=None,
            source_mtime=None,
            custom_title="No Date Session",
        )
        name = generate_filename(s)
        assert name == "undated - No Date Session.md"
        assert not name.startswith(" - ")

    def test_no_empty_date_prefix(self):
        """The leading ' - ' bug must not appear."""
        s = _make_session(created_at=None, modified_at=None, source_mtime=None)
        name = generate_filename(s)
        assert not name.startswith(" - ")
        assert not name.startswith("- ")


# ── autotitle / _prompt_topics ────────────────────────────────────────

class TestPromptTopics:
    def test_rejects_numeric_phrases(self):
        """Numeric strings like '001' must not appear in topics."""
        topics = _prompt_topics("001 Background analysis of the dataset")
        joined = " ".join(topics).lower()
        assert "001" not in joined

    def test_rejects_short_single_cap_words(self):
        """Single capitalized words under 5 chars must not become topics."""
        topics = _prompt_topics("He Chunks some data for analysis")
        joined = " ".join(topics).lower()
        assert "he" not in joined
        # "Chunks" is 6 chars but is a generic verb — check it doesn't dominate
        # The key is 'He' should never appear
        assert not any(t == "He" for t in topics)

    def test_accepts_multi_word_phrases(self):
        """Multi-word capitalized phrases should be captured."""
        topics = _prompt_topics("How does Hypothesis Engine handle session files?")
        joined = " ".join(t.lower() for t in topics)
        assert "hypothesis engine" in joined or "hypothesis" in joined

    def test_strips_caveat_prefix(self):
        """Caveat: system injections should be stripped before topic extraction."""
        prompt = "Caveat: DO NOT respond to this context unless it is highly relevant. Please help me with the Hypothesis Engine pipeline."
        topics = _prompt_topics(prompt)
        joined = " ".join(t.lower() for t in topics)
        assert "caveat" not in joined
        assert "hypothesis" in joined or "engine" in joined or "pipeline" in joined

    def test_strips_do_not_prefix(self):
        """DO NOT respond prefix blocks should be stripped."""
        prompt = "DO NOT respond to this context. Fix the BambuLab printer installation script."
        topics = _prompt_topics(prompt)
        joined = " ".join(t.lower() for t in topics)
        assert "bambulab" in joined or "printer" in joined

    def test_normal_case(self):
        """Clean first prompts produce meaningful topics."""
        topics = _prompt_topics("How do I configure the arXiv pipeline with Gemini?")
        assert len(topics) > 0
        joined = " ".join(t.lower() for t in topics)
        assert any(w in joined for w in ["arxiv", "pipeline", "gemini", "configure"])

    def test_empty_prompt(self):
        """Empty or whitespace prompt returns empty list."""
        assert _prompt_topics("") == []
        assert _prompt_topics("   ") == []


class TestGenerateAutoTitle:
    def test_returns_string(self):
        s = _make_session(
            project_path="/Users/test/claude-sandbox",
            first_prompt="How do I fix this Python script?",
        )
        result = generate_auto_title(s)
        assert isinstance(result, str)

    def test_empty_session(self):
        s = _make_session()
        result = generate_auto_title(s)
        assert isinstance(result, str)  # May be empty — that's ok


# ── _clean_first_prompt ───────────────────────────────────────────────

class TestCleanFirstPrompt:
    def test_strips_xml_tags(self):
        result = _clean_first_prompt("<system>context</system> Fix the bug")
        assert "<system>" not in result
        assert "Fix the bug" in result

    def test_strips_caveat_prefix(self):
        prompt = "Caveat: DO NOT respond to this context unless relevant. Help me with the build."
        result = _clean_first_prompt(prompt)
        assert "caveat" not in result.lower()
        assert "build" in result.lower()

    def test_preserves_real_content(self):
        prompt = "Can you help me refactor the database module?"
        result = _clean_first_prompt(prompt)
        assert "refactor" in result
        assert "database" in result

    def test_fallback_on_total_strip(self):
        """If stripping removes everything, return original."""
        prompt = "Caveat: DO NOT respond to this context unless relevant."
        result = _clean_first_prompt(prompt)
        assert result  # not empty


# ── Parser robustness ─────────────────────────────────────────────────

class TestParser:
    def test_corrupt_jsonl_lines_skipped(self, tmp_path):
        """Files with some corrupt JSON lines parse without crashing."""
        f = tmp_path / "test-session.jsonl"
        lines = [
            json.dumps({"type": "user", "sessionId": "abc123", "message": {"content": "Hello"}, "uuid": "u1", "parentUuid": None}),
            "THIS IS NOT JSON {{{",
            json.dumps({"type": "assistant", "sessionId": "abc123", "message": {"content": [{"type": "text", "text": "Hi"}], "model": "claude-sonnet-4-6", "usage": {}}, "uuid": "u2", "parentUuid": "u1"}),
        ]
        f.write_text("\n".join(lines), encoding="utf-8")
        session = parse_session_file(f)
        assert session is not None
        assert session.user_message_count >= 1

    def test_empty_file_no_crash(self, tmp_path):
        """Zero-byte session file returns empty session without crashing."""
        f = tmp_path / "empty-session.jsonl"
        f.write_text("", encoding="utf-8")
        session = parse_session_file(f)
        assert session is not None
        assert session.user_message_count == 0

    def test_utf8_replacement_no_crash(self, tmp_path):
        """Files with non-UTF8 bytes are handled without crashing."""
        f = tmp_path / "bad-encoding-session.jsonl"
        # Write valid JSON line, then some non-UTF8 bytes, then another valid line
        valid_line = json.dumps({
            "type": "user", "sessionId": "enc123",
            "message": {"content": "test"}, "uuid": "u1", "parentUuid": None
        }).encode("utf-8")
        bad_bytes = b"\x80\x81\x82\x83"  # invalid UTF-8 sequence
        f.write_bytes(valid_line + b"\n" + bad_bytes + b"\n")
        # Should not raise
        session = parse_session_file(f)
        assert session is not None

    def test_sidechain_entries_dont_appear_as_turns(self, tmp_path):
        """isSidechain=True entries should not become conversation turns."""
        f = _write_jsonl(tmp_path, [
            {"type": "user", "sessionId": "s1", "message": {"content": "Real message"}, "uuid": "u1", "parentUuid": None},
            {"type": "assistant", "isSidechain": True, "sessionId": "agent-123", "message": {"content": [], "model": "claude-sonnet-4-6", "usage": {}}, "uuid": "u2", "parentUuid": "u1"},
        ])
        session = parse_session_file(f)
        assert session.user_message_count == 1
        assert session.assistant_message_count == 0  # sidechain not counted

    def test_child_agent_ids_extracted(self, tmp_path):
        """Sidechain entries populate child_agent_ids on the session."""
        f = _write_jsonl(tmp_path, [
            {"type": "user", "sessionId": "parent-uuid", "message": {"content": "Run task"}, "uuid": "u1", "parentUuid": None},
            {"type": "user", "isSidechain": True, "sessionId": "agent-uuid-123", "message": {"content": "sub"}, "uuid": "u3", "parentUuid": None},
        ])
        session = parse_session_file(f)
        assert "agent-uuid-123" in session.child_agent_ids

    def test_first_prompt_strips_system_prefix(self, tmp_path):
        """first_prompt stored in session should not start with CC system injection."""
        prompt_text = "Caveat: DO NOT respond to this context unless relevant. Fix the parser bug."
        f = _write_jsonl(tmp_path, [
            {"type": "user", "sessionId": "s1", "message": {"content": prompt_text}, "uuid": "u1", "parentUuid": None},
        ])
        session = parse_session_file(f)
        assert session.first_prompt
        assert not session.first_prompt.lower().startswith("caveat")
        assert "parser" in session.first_prompt.lower() or "fix" in session.first_prompt.lower()


# ── Database JSON safety ──────────────────────────────────────────────

class TestDatabaseJSONSafety:
    def test_corrupt_tags_json_in_add_tags(self, tmp_path):
        """add_tags handles corrupt tags_json without crashing."""
        import sqlite3
        from ccfolio.database import Database
        db = Database(tmp_path / "test.db")

        # Insert a session with corrupt tags_json directly
        db.conn.execute(
            "INSERT INTO sessions (session_id, tags_json, source_mtime, indexed_at) "
            "VALUES ('s1', 'NOTJSON', 1.0, '2026-01-01')"
        )
        db.conn.commit()

        # Should not raise
        result = db.add_tags("s1", ["newtag"])
        assert "newtag" in result

    def test_corrupt_tags_json_in_remove_tags(self, tmp_path):
        """remove_tags handles corrupt tags_json without crashing."""
        from ccfolio.database import Database
        db = Database(tmp_path / "test.db")

        db.conn.execute(
            "INSERT INTO sessions (session_id, tags_json, source_mtime, indexed_at) "
            "VALUES ('s2', 'NOTJSON', 1.0, '2026-01-01')"
        )
        db.conn.commit()

        # Should not raise
        result = db.remove_tags("s2", ["sometag"])
        assert isinstance(result, list)
