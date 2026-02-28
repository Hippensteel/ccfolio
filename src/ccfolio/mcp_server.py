"""MCP server exposing ccfolio's search and session data."""

from __future__ import annotations

import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ccfolio.config import Config
from ccfolio.database import Database
from ccfolio.pricing import get_model_family


def create_server(config: Config) -> FastMCP:
    """Create and configure the ccfolio MCP server."""
    server = FastMCP(
        "ccfolio",
        instructions="Search and explore Claude Code conversation history. Use these tools to find past sessions, search conversations, and track costs.",
    )

    def _get_db() -> Database:
        return Database(config.db_path)

    @server.tool()
    def search_sessions(query: str, limit: int = 10) -> str:
        """Search across all Claude Code conversations (full-text + substring).

        Searches user messages, assistant responses, summaries, titles, slugs,
        and file paths. Supports both whole-word and partial/substring matching.
        For example, searching "folio" will find sessions containing "ccfolio".

        Args:
            query: Search terms. Partial words work. Multi-word queries match
                   each word independently (e.g. "CC Folio" finds "ccfolio").
            limit: Max results to return (default 10)
        """
        db = _get_db()
        try:
            results = db.search(query, limit=limit)
            if not results:
                return f"No sessions found matching '{query}'"

            lines = [f"Found {len(results)} sessions matching '{query}':\n"]
            for i, r in enumerate(results, 1):
                date = r["created_at"][:10] if r["created_at"] else "unknown"
                title = r["custom_title"] or r["summary"] or (r["first_prompt"] or "")[:60]
                model = get_model_family(r["primary_model"]) if r["primary_model"] else "?"
                cost = f"${r['estimated_cost_usd']:.2f}" if r["estimated_cost_usd"] else "$0"
                snippet = (r.get("snippet") or "")[:120]
                # Clean FTS markers
                snippet = snippet.replace(">>>", "**").replace("<<<", "**")

                lines.append(
                    f"{i}. [{date}] {title}\n"
                    f"   Model: {model} | Cost: {cost} | ID: {r['session_id'][:8]}\n"
                    f"   {snippet}"
                )
            return "\n".join(lines)
        finally:
            db.close()

    @server.tool()
    def list_recent_sessions(count: int = 15, sort_by: str = "date") -> str:
        """List recent Claude Code sessions.

        Args:
            count: Number of sessions to return (default 15)
            sort_by: Sort order - "date" (newest first), "cost" (most expensive), "messages", or "tokens"
        """
        db = _get_db()
        try:
            sessions = db.list_sessions(sort_by=sort_by, limit=count)
            if not sessions:
                return "No sessions indexed."

            lines = [f"Recent {len(sessions)} sessions (sorted by {sort_by}):\n"]
            for i, s in enumerate(sessions, 1):
                date = s["created_at"][:10] if s["created_at"] else "unknown"
                title = s["custom_title"] or s["summary"] or (s["first_prompt"] or "")[:60]
                msgs = s["user_message_count"] + s["assistant_message_count"]
                model = get_model_family(s["primary_model"]) if s["primary_model"] else "?"
                cost = f"${s['estimated_cost_usd']:.2f}" if s["estimated_cost_usd"] else "$0"
                fav = " *" if s["is_favorited"] else ""
                slug = s["slug"] or ""

                lines.append(
                    f"{i}. [{date}] {title}{fav}\n"
                    f"   {msgs} msgs | {model} | {cost} | {slug} | {s['session_id'][:8]}"
                )
            return "\n".join(lines)
        finally:
            db.close()

    @server.tool()
    def find_sessions_for_file(file_path: str) -> str:
        """Find all sessions that read, wrote, or edited a file.

        Use this to find past conversations that touched a specific file.
        Partial paths work (e.g., "CLAUDE.md" matches any path containing it).

        Args:
            file_path: Full or partial file path to search for
        """
        db = _get_db()
        try:
            sessions = db.find_sessions_for_file(file_path)
            if not sessions:
                return f"No sessions found that touched '{file_path}'"

            lines = [f"Found {len(sessions)} sessions touching '{file_path}':\n"]
            for i, s in enumerate(sessions, 1):
                date = s["created_at"][:10] if s["created_at"] else "unknown"
                title = s["custom_title"] or s["summary"] or (s["first_prompt"] or "")[:60]
                model = get_model_family(s["primary_model"]) if s["primary_model"] else "?"
                cost = f"${s['estimated_cost_usd']:.2f}" if s["estimated_cost_usd"] else "$0"

                lines.append(
                    f"{i}. [{date}] {title}\n"
                    f"   Model: {model} | Cost: {cost} | ID: {s['session_id'][:8]}"
                )
            return "\n".join(lines)
        finally:
            db.close()

    @server.tool()
    def get_session_details(session_id: str) -> str:
        """Get detailed information about a specific session.

        Args:
            session_id: Full session UUID, UUID prefix, or slug
        """
        db = _get_db()
        try:
            resolved = db.resolve_session_id(session_id)
            if not resolved:
                return f"Session not found: {session_id}"

            s = db.get_session(resolved)
            if not s:
                return f"Session not found: {resolved}"

            title = s["custom_title"] or s["summary"] or (s["first_prompt"] or "")[:80]
            date = s["created_at"][:10] if s["created_at"] else "unknown"
            model = get_model_family(s["primary_model"]) if s["primary_model"] else "?"
            msgs = s["user_message_count"] + s["assistant_message_count"]
            cost = f"${s['estimated_cost_usd']:.2f}" if s["estimated_cost_usd"] else "$0"
            try:
                tags = json.loads(s["tags_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                tags = []
            try:
                tools = json.loads(s["tool_calls_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                tools = {}
            try:
                files = json.loads(s["files_touched_json"] or "[]")
            except (json.JSONDecodeError, TypeError):
                files = []

            top_tools = sorted(tools.items(), key=lambda x: x[1], reverse=True)[:5]
            tool_str = ", ".join(f"{n}: {c}" for n, c in top_tools) if top_tools else "none"

            lines = [
                f"Session: {title}",
                f"ID: {s['session_id']}",
                f"Slug: {s['slug'] or 'none'}",
                f"Date: {date}",
                f"Model: {model} ({s['primary_model']})",
                f"Messages: {msgs} ({s['user_message_count']} user, {s['assistant_message_count']} assistant)",
                f"Tool calls: {s['tool_call_count']} ({tool_str})",
                f"Tokens: {s['total_input_tokens']:,} in, {s['total_output_tokens']:,} out",
                f"Cost: {cost}",
                f"Project: {s['project_path'] or 'unknown'}",
                f"Favorite: {'yes' if s['is_favorited'] else 'no'}",
                f"Tags: {', '.join(tags) if tags else 'none'}",
            ]

            if files:
                lines.append(f"Files touched: {len(files)}")
                for f in files[:10]:
                    lines.append(f"  - {f}")
                if len(files) > 10:
                    lines.append(f"  ... and {len(files) - 10} more")

            if s["first_prompt"]:
                prompt = s["first_prompt"][:200]
                lines.append(f"\nFirst prompt: {prompt}")

            return "\n".join(lines)
        finally:
            db.close()

    @server.tool()
    def get_cost_summary(period: str = "daily", after: str | None = None) -> str:
        """Get cost breakdown for Claude Code usage.

        Args:
            period: Group by "daily", "monthly", "model", or "project"
            after: Only include sessions after this date (YYYY-MM-DD)
        """
        db = _get_db()
        try:
            rows = db.get_cost_summary(group_by=period, after=after)
            if not rows:
                return "No cost data found."

            filtered = [r for r in rows if r["total_cost"]]
            if not filtered:
                return "No cost data found (all $0)."

            total_cost = sum(r["total_cost"] or 0 for r in filtered)
            total_sessions = sum(r["session_count"] or 0 for r in filtered)

            lines = [f"Cost breakdown by {period} ({total_sessions} sessions, ${total_cost:.2f} total):\n"]
            for r in filtered[:20]:
                lines.append(
                    f"  {r['period']}: {r['session_count']} sessions, "
                    f"${r['total_cost']:.2f} "
                    f"({r['input_tokens'] or 0:,} in, {r['output_tokens'] or 0:,} out)"
                )
            return "\n".join(lines)
        finally:
            db.close()

    return server
