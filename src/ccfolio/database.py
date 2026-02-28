"""SQLite database operations for Claude Chronicle."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ccfolio.models import Session, TokenUsage

SCHEMA_VERSION = 2

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    slug TEXT,
    custom_title TEXT DEFAULT '',
    project_path TEXT,
    project_encoded TEXT,
    cwd TEXT,
    git_branch TEXT,
    created_at TEXT,
    modified_at TEXT,
    duration_ms INTEGER DEFAULT 0,
    first_prompt TEXT,
    summary TEXT DEFAULT '',
    user_message_count INTEGER DEFAULT 0,
    assistant_message_count INTEGER DEFAULT 0,
    tool_call_count INTEGER DEFAULT 0,
    tool_calls_json TEXT DEFAULT '{}',
    models_used_json TEXT DEFAULT '[]',
    primary_model TEXT DEFAULT '',
    total_input_tokens INTEGER DEFAULT 0,
    total_output_tokens INTEGER DEFAULT 0,
    total_cache_creation_tokens INTEGER DEFAULT 0,
    total_cache_read_tokens INTEGER DEFAULT 0,
    estimated_cost_usd REAL DEFAULT 0.0,
    files_touched_json TEXT DEFAULT '[]',
    subagent_count INTEGER DEFAULT 0,
    child_agent_ids_json TEXT DEFAULT '[]',
    cc_version TEXT DEFAULT '',
    permission_mode TEXT DEFAULT '',
    is_favorited INTEGER DEFAULT 0,
    tags_json TEXT DEFAULT '[]',
    source_file TEXT,
    source_mtime REAL,
    indexed_at TEXT,
    exported_at TEXT DEFAULT NULL
);

CREATE TABLE IF NOT EXISTS files_touched (
    session_id TEXT,
    file_path TEXT,
    operation TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_files_path ON files_touched(file_path);
CREATE INDEX IF NOT EXISTS idx_files_session ON files_touched(session_id);

CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_project ON sessions(project_path);
CREATE INDEX IF NOT EXISTS idx_sessions_model ON sessions(primary_model);
CREATE INDEX IF NOT EXISTS idx_sessions_favorited ON sessions(is_favorited);

CREATE TABLE IF NOT EXISTS session_agents (
    agent_id TEXT PRIMARY KEY,
    agent_session_id TEXT DEFAULT '',
    parent_session_id TEXT,
    task_description TEXT DEFAULT '',
    subagent_type TEXT DEFAULT '',
    first_prompt TEXT DEFAULT '',
    content_text TEXT DEFAULT '',
    source_file TEXT,
    source_mtime REAL,
    indexed_at TEXT,
    FOREIGN KEY (parent_session_id) REFERENCES sessions(session_id)
);

CREATE INDEX IF NOT EXISTS idx_agents_parent ON session_agents(parent_session_id);
CREATE INDEX IF NOT EXISTS idx_agents_session_id ON session_agents(agent_session_id);

CREATE VIRTUAL TABLE IF NOT EXISTS sessions_fts USING fts5(
    session_id,
    first_prompt,
    summary,
    content,
    files_touched,
    tokenize='porter unicode61'
);
"""


class Database:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.conn = sqlite3.connect(str(db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create tables if they don't exist."""
        cursor = self.conn.cursor()

        # Check if schema exists
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_version'"
        )
        if not cursor.fetchone():
            cursor.executescript(SCHEMA_SQL)
            cursor.execute(
                "INSERT OR REPLACE INTO schema_version (version) VALUES (?)",
                (SCHEMA_VERSION,),
            )
            self.conn.commit()
        else:
            # Migrations for existing databases
            self._migrate(cursor)

    def _migrate(self, cursor: sqlite3.Cursor) -> None:
        """Run schema migrations."""
        columns = [
            row[1] for row in cursor.execute("PRAGMA table_info(sessions)").fetchall()
        ]

        # v1: Add exported_at
        if "exported_at" not in columns:
            cursor.execute("ALTER TABLE sessions ADD COLUMN exported_at TEXT DEFAULT NULL")

        # v2: Add child_agent_ids_json + session_agents table
        if "child_agent_ids_json" not in columns:
            cursor.execute(
                "ALTER TABLE sessions ADD COLUMN child_agent_ids_json TEXT DEFAULT '[]'"
            )

        cursor.execute(
            """CREATE TABLE IF NOT EXISTS session_agents (
                agent_id TEXT PRIMARY KEY,
                agent_session_id TEXT DEFAULT '',
                parent_session_id TEXT,
                task_description TEXT DEFAULT '',
                subagent_type TEXT DEFAULT '',
                first_prompt TEXT DEFAULT '',
                content_text TEXT DEFAULT '',
                source_file TEXT,
                source_mtime REAL,
                indexed_at TEXT,
                FOREIGN KEY (parent_session_id) REFERENCES sessions(session_id)
            )"""
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_parent ON session_agents(parent_session_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_agents_session_id ON session_agents(agent_session_id)"
        )

        # Remove any orphaned agent files that were incorrectly indexed as sessions
        cursor.execute(
            "DELETE FROM sessions_fts WHERE session_id IN "
            "(SELECT session_id FROM sessions WHERE source_file LIKE '%/agent-%')"
        )
        cursor.execute(
            "DELETE FROM files_touched WHERE session_id IN "
            "(SELECT session_id FROM sessions WHERE source_file LIKE '%/agent-%')"
        )
        cursor.execute(
            "DELETE FROM sessions WHERE source_file LIKE '%/agent-%'"
        )

        self.conn.commit()

    def get_session_mtime(self, session_id: str) -> float | None:
        """Get stored mtime for a session, or None if not indexed."""
        cursor = self.conn.execute(
            "SELECT source_mtime FROM sessions WHERE session_id = ?",
            (session_id,),
        )
        row = cursor.fetchone()
        return row["source_mtime"] if row else None

    def upsert_session(self, session: Session, content_text: str = "") -> None:
        """Insert or update a session in the database."""
        now = datetime.utcnow().isoformat() + "Z"

        # Preserve favorites and tags from existing record
        existing = self.conn.execute(
            "SELECT is_favorited, tags_json FROM sessions WHERE session_id = ?",
            (session.session_id,),
        ).fetchone()

        is_favorited = session.is_favorited
        tags_json = json.dumps(session.tags)
        if existing:
            is_favorited = bool(existing["is_favorited"])
            tags_json = existing["tags_json"]

        try:
            self.conn.execute(
                """INSERT OR REPLACE INTO sessions (
                    session_id, slug, custom_title, project_path, project_encoded,
                    cwd, git_branch, created_at, modified_at, duration_ms,
                    first_prompt, summary,
                    user_message_count, assistant_message_count, tool_call_count,
                    tool_calls_json, models_used_json, primary_model,
                    total_input_tokens, total_output_tokens,
                    total_cache_creation_tokens, total_cache_read_tokens,
                    estimated_cost_usd, files_touched_json,
                    subagent_count, child_agent_ids_json, cc_version, permission_mode,
                    is_favorited, tags_json,
                    source_file, source_mtime, indexed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )""",
                (
                    session.session_id,
                    session.slug,
                    session.custom_title,
                    session.project_path,
                    session.project_encoded,
                    session.cwd,
                    session.git_branch,
                    session.created_at.isoformat() if session.created_at else None,
                    session.modified_at.isoformat() if session.modified_at else None,
                    session.duration_ms,
                    session.first_prompt,
                    session.summary,
                    session.user_message_count,
                    session.assistant_message_count,
                    session.tool_call_count,
                    json.dumps(session.tool_calls_by_name),
                    json.dumps(session.models_used),
                    session.primary_model,
                    session.total_usage.input_tokens,
                    session.total_usage.output_tokens,
                    session.total_usage.cache_creation_tokens,
                    session.total_usage.cache_read_tokens,
                    session.estimated_cost_usd,
                    json.dumps(session.files_touched),
                    session.subagent_count,
                    json.dumps(session.child_agent_ids),
                    session.cc_version,
                    session.permission_mode,
                    int(is_favorited),
                    tags_json,
                    session.source_file,
                    session.source_mtime,
                    now,
                ),
            )

            # Update files_touched table
            self.conn.execute(
                "DELETE FROM files_touched WHERE session_id = ?",
                (session.session_id,),
            )
            for path in session.files_read:
                self._insert_file_touched(session.session_id, path, "read")
            for path in session.files_written:
                self._insert_file_touched(session.session_id, path, "write")
            for path in session.files_edited:
                self._insert_file_touched(session.session_id, path, "edit")

            # Update FTS index
            self.conn.execute(
                "DELETE FROM sessions_fts WHERE session_id = ?",
                (session.session_id,),
            )
            self.conn.execute(
                """INSERT INTO sessions_fts (session_id, first_prompt, summary, content, files_touched)
                VALUES (?, ?, ?, ?, ?)""",
                (
                    session.session_id,
                    session.first_prompt,
                    session.summary,
                    content_text,
                    " ".join(session.files_touched),
                ),
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise

    def _insert_file_touched(self, session_id: str, path: str, operation: str) -> None:
        self.conn.execute(
            "INSERT INTO files_touched (session_id, file_path, operation) VALUES (?, ?, ?)",
            (session_id, path, operation),
        )

    def append_agent_content_to_fts(self, session_id: str, agent_content: str) -> None:
        """Append agent content to an existing session's FTS entry."""
        row = self.conn.execute(
            "SELECT content FROM sessions_fts WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            return
        existing = row["content"] or ""
        combined = (existing + "\n\n" + agent_content).strip()
        # Respect the 50K char limit
        if len(combined) > 50000:
            combined = combined[:50000]
        self.conn.execute(
            "DELETE FROM sessions_fts WHERE session_id = ?",
            (session_id,),
        )
        # Re-fetch the other FTS fields from the main table
        sess_row = self.conn.execute(
            "SELECT first_prompt, summary, files_touched_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if sess_row:
            files_str = " ".join(json.loads(sess_row["files_touched_json"] or "[]"))
            self.conn.execute(
                """INSERT INTO sessions_fts (session_id, first_prompt, summary, content, files_touched)
                VALUES (?, ?, ?, ?, ?)""",
                (session_id, sess_row["first_prompt"], sess_row["summary"], combined, files_str),
            )
        self.conn.commit()

    def get_agent_mtime(self, agent_id: str) -> float | None:
        """Get stored mtime for an agent file, or None if not indexed."""
        row = self.conn.execute(
            "SELECT source_mtime FROM session_agents WHERE agent_id = ?",
            (agent_id,),
        ).fetchone()
        return row["source_mtime"] if row else None

    def get_parent_session_id_for_agent(self, agent_session_id: str) -> str | None:
        """Find the parent session that has this agent's session ID in its child_agent_ids."""
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE child_agent_ids_json LIKE ?",
            (f'%{agent_session_id}%',),
        ).fetchone()
        return row["session_id"] if row else None

    def upsert_agent(
        self,
        agent_id: str,
        agent_session_id: str,
        parent_session_id: str | None,
        task_description: str,
        subagent_type: str,
        first_prompt: str,
        content_text: str,
        source_file: str,
        source_mtime: float,
    ) -> None:
        """Insert or update a subagent record."""
        now = datetime.utcnow().isoformat() + "Z"
        self.conn.execute(
            """INSERT OR REPLACE INTO session_agents (
                agent_id, agent_session_id, parent_session_id,
                task_description, subagent_type, first_prompt,
                content_text, source_file, source_mtime, indexed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                agent_id, agent_session_id, parent_session_id,
                task_description, subagent_type, first_prompt,
                content_text, source_file, source_mtime, now,
            ),
        )
        self.conn.commit()

    def get_agents_for_session(self, session_id: str) -> list[dict]:
        """Return all subagents linked to a parent session."""
        rows = self.conn.execute(
            """SELECT agent_id, task_description, subagent_type, first_prompt
               FROM session_agents WHERE parent_session_id = ?
               ORDER BY agent_id""",
            (session_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_sessions(
        self,
        project: str | None = None,
        model: str | None = None,
        favorites_only: bool = False,
        tag: str | None = None,
        after: str | None = None,
        before: str | None = None,
        sort_by: str = "date",
        limit: int | None = None,
    ) -> list[dict]:
        """List sessions with optional filters."""
        conditions = []
        params: list = []

        if project:
            conditions.append("project_path LIKE ?")
            params.append(f"%{project}%")
        if model:
            conditions.append("(primary_model LIKE ? OR models_used_json LIKE ?)")
            params.extend([f"%{model}%", f"%{model}%"])
        if favorites_only:
            conditions.append("is_favorited = 1")
        if tag:
            conditions.append("tags_json LIKE ?")
            params.append(f'%"{tag}"%')
        if after:
            conditions.append("created_at >= ?")
            params.append(after)
        if before:
            conditions.append("created_at <= ?")
            params.append(before)

        where = " AND ".join(conditions) if conditions else "1=1"

        sort_map = {
            "date": "created_at DESC",
            "cost": "estimated_cost_usd DESC",
            "messages": "(user_message_count + assistant_message_count) DESC",
            "tokens": "(total_input_tokens + total_output_tokens) DESC",
        }
        order = sort_map.get(sort_by, "created_at DESC")

        query = f"SELECT * FROM sessions WHERE {where} ORDER BY {order}"
        if limit:
            query += f" LIMIT {limit}"

        return [dict(row) for row in self.conn.execute(query, params).fetchall()]

    def search(self, query: str, limit: int = 20) -> list[dict]:
        """Search across sessions using FTS5 + substring fallback.

        FTS5 handles whole-word and stemmed matches well but cannot do
        substring matching (e.g. "folio" won't find "ccfolio"). We always
        run both FTS5 and LIKE-based substring search, then merge results
        with FTS hits first (ranked by relevance), followed by LIKE-only
        hits (ranked by date).
        """
        seen: set[str] = set()
        merged: list[dict] = []

        # 1) FTS5 full-text search (best ranking for whole-word matches)
        try:
            fts_rows = self.conn.execute(
                """SELECT s.*, highlight(sessions_fts, 3, '>>>', '<<<') as snippet
                FROM sessions_fts fts
                JOIN sessions s ON s.session_id = fts.session_id
                WHERE sessions_fts MATCH ?
                ORDER BY rank
                LIMIT ?""",
                (query, limit),
            ).fetchall()
            for row in fts_rows:
                d = dict(row)
                seen.add(d["session_id"])
                merged.append(d)
        except sqlite3.OperationalError:
            # FTS query syntax error (e.g. special chars) — skip FTS
            pass

        # 2) LIKE-based substring search across key text fields.
        # For multi-word queries, each word must match independently so
        # "CC Folio" finds "ccfolio" (contains both "cc" and "folio").
        words = query.split()
        if not words:
            return merged[:limit]

        # Build per-word conditions: each word must appear in at least one field
        word_clauses = []
        like_params: list[str] = []
        for word in words:
            pat = f"%{word}%"
            word_clauses.append(
                "(s.custom_title LIKE ? OR s.first_prompt LIKE ? "
                "OR s.summary LIKE ? OR s.slug LIKE ? "
                "OR fts.content LIKE ? OR fts.files_touched LIKE ?)"
            )
            like_params.extend([pat] * 6)

        where_clause = " AND ".join(word_clauses)
        like_params.append(str(limit))

        like_rows = self.conn.execute(
            f"""SELECT s.*, '' as snippet
            FROM sessions s
            LEFT JOIN sessions_fts fts ON s.session_id = fts.session_id
            WHERE {where_clause}
            ORDER BY s.created_at DESC
            LIMIT ?""",
            like_params,
        ).fetchall()
        for row in like_rows:
            d = dict(row)
            if d["session_id"] not in seen:
                seen.add(d["session_id"])
                merged.append(d)

        return merged[:limit]

    def toggle_favorite(self, session_id: str) -> bool:
        """Toggle favorite status. Returns new state."""
        row = self.conn.execute(
            "SELECT is_favorited FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Session not found: {session_id}")
        new_state = not bool(row["is_favorited"])
        self.conn.execute(
            "UPDATE sessions SET is_favorited = ? WHERE session_id = ?",
            (int(new_state), session_id),
        )
        self.conn.commit()
        return new_state

    def add_tags(self, session_id: str, tags: list[str]) -> list[str]:
        """Add tags to a session. Returns updated tag list."""
        row = self.conn.execute(
            "SELECT tags_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Session not found: {session_id}")
        try:
            current = json.loads(row["tags_json"] or "[]")
        except json.JSONDecodeError:
            current = []
        updated = sorted(set(current + tags))
        self.conn.execute(
            "UPDATE sessions SET tags_json = ? WHERE session_id = ?",
            (json.dumps(updated), session_id),
        )
        self.conn.commit()
        return updated

    def remove_tags(self, session_id: str, tags: list[str]) -> list[str]:
        """Remove tags from a session. Returns updated tag list."""
        row = self.conn.execute(
            "SELECT tags_json FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if not row:
            raise ValueError(f"Session not found: {session_id}")
        try:
            current = json.loads(row["tags_json"] or "[]")
        except json.JSONDecodeError:
            current = []
        updated = sorted(set(current) - set(tags))
        self.conn.execute(
            "UPDATE sessions SET tags_json = ? WHERE session_id = ?",
            (json.dumps(updated), session_id),
        )
        self.conn.commit()
        return updated

    def resolve_session_id(self, identifier: str) -> str | None:
        """Resolve a flexible session identifier to a full session_id.

        Accepts: full UUID, UUID prefix, slug, or #N index.
        """
        # Full UUID
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE session_id = ?",
            (identifier,),
        ).fetchone()
        if row:
            return row["session_id"]

        # UUID prefix
        rows = self.conn.execute(
            "SELECT session_id FROM sessions WHERE session_id LIKE ?",
            (f"{identifier}%",),
        ).fetchall()
        if len(rows) == 1:
            return rows[0]["session_id"]

        # Slug match
        row = self.conn.execute(
            "SELECT session_id FROM sessions WHERE slug = ?",
            (identifier,),
        ).fetchone()
        if row:
            return row["session_id"]

        # Index number (#N)
        if identifier.startswith("#"):
            try:
                idx = int(identifier[1:]) - 1
                row = self.conn.execute(
                    "SELECT session_id FROM sessions ORDER BY created_at DESC LIMIT 1 OFFSET ?",
                    (idx,),
                ).fetchone()
                if row:
                    return row["session_id"]
            except ValueError:
                pass

        return None

    def get_session(self, session_id: str) -> dict | None:
        """Get a single session by ID."""
        row = self.conn.execute(
            "SELECT * FROM sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_cost_summary(
        self,
        group_by: str = "daily",
        model: str | None = None,
        project: str | None = None,
        after: str | None = None,
        before: str | None = None,
    ) -> list[dict]:
        """Get cost aggregation."""
        date_expr = {
            "daily": "DATE(created_at)",
            "monthly": "STRFTIME('%Y-%m', created_at)",
            "model": "primary_model",
            "project": "project_path",
        }.get(group_by, "DATE(created_at)")

        conditions = ["created_at IS NOT NULL"]
        params: list = []

        if model:
            conditions.append("primary_model LIKE ?")
            params.append(f"%{model}%")
        if project:
            conditions.append("project_path LIKE ?")
            params.append(f"%{project}%")
        if after:
            conditions.append("created_at >= ?")
            params.append(after)
        if before:
            conditions.append("created_at <= ?")
            params.append(before)

        where = " AND ".join(conditions)

        rows = self.conn.execute(
            f"""SELECT {date_expr} as period,
                COUNT(*) as session_count,
                SUM(total_input_tokens) as input_tokens,
                SUM(total_output_tokens) as output_tokens,
                SUM(total_cache_creation_tokens) as cache_creation_tokens,
                SUM(total_cache_read_tokens) as cache_read_tokens,
                SUM(estimated_cost_usd) as total_cost
            FROM sessions
            WHERE {where}
            GROUP BY {date_expr}
            ORDER BY period DESC""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def get_stats(self) -> dict:
        """Get overall statistics."""
        row = self.conn.execute(
            """SELECT
                COUNT(*) as total_sessions,
                SUM(user_message_count) as total_user_messages,
                SUM(assistant_message_count) as total_assistant_messages,
                SUM(tool_call_count) as total_tool_calls,
                SUM(total_input_tokens) as total_input_tokens,
                SUM(total_output_tokens) as total_output_tokens,
                SUM(estimated_cost_usd) as total_cost,
                MIN(created_at) as earliest_session,
                MAX(created_at) as latest_session,
                SUM(is_favorited) as favorite_count
            FROM sessions"""
        ).fetchone()
        return dict(row)

    def get_tool_stats(self) -> list[tuple[str, int]]:
        """Get tool usage counts across all sessions."""
        rows = self.conn.execute("SELECT tool_calls_json FROM sessions").fetchall()
        totals: dict[str, int] = {}
        for row in rows:
            tools = json.loads(row["tool_calls_json"])
            for name, count in tools.items():
                totals[name] = totals.get(name, 0) + count
        return sorted(totals.items(), key=lambda x: x[1], reverse=True)

    def get_files_for_session(self, session_id: str) -> list[dict]:
        """Get files touched by a session with operations."""
        rows = self.conn.execute(
            "SELECT file_path, operation FROM files_touched WHERE session_id = ? ORDER BY file_path",
            (session_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def find_sessions_for_file(self, file_path: str) -> list[dict]:
        """Find sessions that touched a given file."""
        rows = self.conn.execute(
            """SELECT DISTINCT s.*
            FROM sessions s
            JOIN files_touched ft ON s.session_id = ft.session_id
            WHERE ft.file_path LIKE ?
            ORDER BY s.created_at DESC""",
            (f"%{file_path}%",),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_sessions_needing_export(self) -> list[dict]:
        """Find sessions that were indexed/updated after their last export."""
        rows = self.conn.execute(
            """SELECT * FROM sessions
            WHERE exported_at IS NULL
               OR indexed_at > exported_at
            ORDER BY created_at DESC"""
        ).fetchall()
        return [dict(row) for row in rows]

    def mark_exported(self, session_id: str) -> None:
        """Mark a session as exported."""
        now = datetime.utcnow().isoformat() + "Z"
        self.conn.execute(
            "UPDATE sessions SET exported_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        self.conn.commit()

    def set_custom_title(self, session_id: str, title: str) -> None:
        """Set a custom title for a session."""
        self.conn.execute(
            "UPDATE sessions SET custom_title = ?, indexed_at = ? WHERE session_id = ?",
            (title, datetime.utcnow().isoformat() + "Z", session_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()
