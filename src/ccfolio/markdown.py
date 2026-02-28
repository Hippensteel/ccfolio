"""Obsidian markdown renderer for Claude Chronicle sessions."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from ccfolio.models import Session, Turn, ToolCall, ToolResult
from ccfolio.pricing import get_model_family


def render_session(
    session: Session,
    vault_path: str = "",
    collapsed_tools: bool = True,
    tool_result_max: int = 500,
    default_tags: list[str] | None = None,
    redact_paths: bool = False,
    subagents: list[dict] | None = None,
) -> str:
    """Render a session as Obsidian-compatible markdown."""
    parts = []
    parts.append(_render_frontmatter(session, default_tags or ["Claude-Session"], redact=redact_paths))
    parts.append("")
    parts.append(f"# {session.title}")
    parts.append("")

    if session.first_prompt:
        prompt_preview = session.first_prompt[:300]
        if len(session.first_prompt) > 300:
            prompt_preview += "..."
        parts.append(f"> **First prompt:** {prompt_preview}")
        parts.append("")

    if session.summary:
        parts.append("## Summary")
        parts.append("")
        parts.append(session.summary)
        parts.append("")

    # Conversation
    parts.append("## Conversation")
    parts.append("")

    # Merge consecutive tool-result user turns with the preceding assistant turn
    merged_turns = _merge_tool_result_turns(session.turns)

    for turn in merged_turns:
        rendered = _render_turn(turn, vault_path, collapsed_tools, tool_result_max)
        if redact_paths:
            rendered = _redact_text(rendered)
        parts.append(rendered)
        parts.append("")

    # Files touched
    if session.files_touched:
        parts.append("---")
        parts.append("")
        parts.append("## Files Touched")
        parts.append("")
        parts.append("| File | Operations |")
        parts.append("|------|-----------|")

        # Build operation map
        op_map: dict[str, list[str]] = {}
        for f in session.files_read:
            op_map.setdefault(f, []).append("Read")
        for f in session.files_written:
            op_map.setdefault(f, []).append("Write")
        for f in session.files_edited:
            op_map.setdefault(f, []).append("Edit")

        for filepath in sorted(session.files_touched):
            display = _path_display(filepath, vault_path, redact=redact_paths)
            ops = ", ".join(op_map.get(filepath, ["Touched"]))
            parts.append(f"| {display} | {ops} |")
        parts.append("")

    # Subagents
    if subagents:
        parts.append("---")
        parts.append("")
        parts.append("## Subagents")
        parts.append("")
        for agent in subagents:
            agent_id = agent.get("agent_id", "")
            desc = agent.get("task_description", "")
            atype = agent.get("subagent_type", "")
            first = agent.get("first_prompt", "")
            header = f"### {agent_id}"
            if desc:
                header += f" — {desc[:80]}"
            parts.append(header)
            if atype:
                parts.append(f"**Type:** {atype}")
            if first:
                preview = first[:200] + ("..." if len(first) > 200 else "")
                parts.append(f"**First prompt:** {preview}")
            parts.append("")

    # Token usage
    parts.append("## Token Usage")
    parts.append("")
    parts.append("| Metric | Value |")
    parts.append("|--------|-------|")
    parts.append(f"| Input | {session.total_usage.input_tokens:,} |")
    parts.append(f"| Output | {session.total_usage.output_tokens:,} |")
    if session.total_usage.cache_creation_tokens:
        parts.append(
            f"| Cache creation | {session.total_usage.cache_creation_tokens:,} |"
        )
    if session.total_usage.cache_read_tokens:
        parts.append(f"| Cache read | {session.total_usage.cache_read_tokens:,} |")
    parts.append(f"| **Estimated cost** | **${session.estimated_cost_usd:.2f}** |")
    parts.append("")

    return "\n".join(parts)


def _render_frontmatter(session: Session, default_tags: list[str], redact: bool = False) -> str:
    """Render YAML frontmatter."""
    lines = ["---"]

    lines.append("type: Claude-Session")
    lines.append(f'session_id: "{session.session_id}"')

    if session.slug:
        lines.append(f"slug: {session.slug}")

    if session.created_at:
        lines.append(f"date: {session.created_at.strftime('%Y-%m-%d')}")
        lines.append(f"created: {session.created_at.isoformat()}")
    if session.modified_at:
        lines.append(f"modified: {session.modified_at.isoformat()}")
    if session.duration_display:
        lines.append(f'duration: "{session.duration_display}"')

    project = _redact_text(session.project_path) if redact else session.project_path
    lines.append(f'project: "{project}"')
    if session.git_branch:
        lines.append(f"git_branch: {session.git_branch}")

    if session.primary_model:
        lines.append(f"model: {session.primary_model}")
    if len(session.models_used) > 1:
        lines.append("models_used:")
        for m in session.models_used:
            lines.append(f"  - {m}")

    if session.cc_version:
        lines.append(f'cc_version: "{session.cc_version}"')

    lines.append(f"messages: {session.user_message_count + session.assistant_message_count}")
    lines.append(f"tool_calls: {session.tool_call_count}")

    if session.tool_calls_by_name:
        lines.append("tools_used:")
        for tool in sorted(session.tool_calls_by_name.keys()):
            lines.append(f"  - {tool}")

    lines.append(f"tokens_in: {session.total_usage.input_tokens}")
    lines.append(f"tokens_out: {session.total_usage.output_tokens}")
    lines.append(f"cost_usd: {session.estimated_cost_usd:.2f}")

    if session.subagent_count:
        lines.append(f"subagents: {session.subagent_count}")

    lines.append(f"favorite: {str(session.is_favorited).lower()}")

    # Tags
    tags = list(default_tags)
    if session.primary_model:
        family = get_model_family(session.primary_model)
        if family != "Unknown":
            tags.append(family)
    tags.extend(session.tags)
    tags = sorted(set(tags))
    lines.append(f"tags: [{', '.join(tags)}]")

    lines.append("---")
    return "\n".join(lines)


def _render_turn(
    turn: Turn,
    vault_path: str,
    collapsed: bool,
    result_max: int,
) -> str:
    """Render a single conversation turn."""
    parts = []
    ts = turn.timestamp.strftime("%H:%M") if turn.timestamp else ""

    if turn.role == "user":
        if turn.text_content.strip():
            parts.append(f"### User ({ts})")
            parts.append("")
            parts.append(turn.text_content.strip())
        # Tool results are rendered inline with the preceding assistant turn
        # via _merge_tool_result_turns, so we skip standalone tool-result-only
        # user turns here.
    else:
        parts.append(f"### Claude ({ts})")
        parts.append("")

        if turn.text_content.strip():
            parts.append(turn.text_content.strip())

        for tc in turn.tool_calls:
            parts.append("")
            parts.append(_render_tool_call(tc, collapsed))

        # Render attached tool results
        for tr in turn.tool_results:
            # These come from the merged user turn
            matching_tc = next(
                (tc for tc in turn.tool_calls if tc.tool_id == tr.tool_use_id),
                None,
            )
            if matching_tc:
                # Result rendered inline with the tool call
                continue

    return "\n".join(parts)


def _render_tool_call(tc: ToolCall, collapsed: bool) -> str:
    """Render a tool call as an Obsidian callout."""
    collapse = "-" if collapsed else "+"
    lines = [f"> [!tool]{collapse} {tc.name}: {tc.input_summary}"]

    # Show relevant input details
    if tc.name == "Bash":
        cmd = tc.input_raw.get("command", "")
        if cmd:
            lines.append("> ```bash")
            for cmd_line in cmd.split("\n"):
                lines.append(f"> {cmd_line}")
            lines.append("> ```")
    elif tc.name in ("Read", "Write", "Edit"):
        path = tc.input_raw.get("file_path", "")
        if path:
            lines.append(f"> `{path}`")
    elif tc.name == "Grep":
        pattern = tc.input_raw.get("pattern", "")
        path = tc.input_raw.get("path", "")
        lines.append(f"> Pattern: `{pattern}`")
        if path:
            lines.append(f"> Path: `{path}`")

    return "\n".join(lines)


def _merge_tool_result_turns(turns: list[Turn]) -> list[Turn]:
    """Merge tool-result-only user turns into the preceding assistant turn.

    When Claude uses tools, the flow is:
    1. Assistant message with tool_use blocks
    2. User message with tool_result blocks (sent automatically by CC)
    3. Next assistant message

    For readability, we attach the tool results from (2) to the assistant turn (1).
    """
    merged = []
    for i, turn in enumerate(turns):
        if turn.role == "user" and not turn.text_content.strip() and turn.tool_results:
            # This is an auto-generated tool result turn
            # Attach results to the previous assistant turn
            if merged and merged[-1].role == "assistant":
                merged[-1].tool_results.extend(turn.tool_results)
                continue
        merged.append(turn)
    return merged


def _redact_text(text: str) -> str:
    """Redact home directory paths and username from text."""
    home = str(Path.home())
    username = Path.home().name
    # Replace full home path with ~
    text = text.replace(home, "~")
    # Replace /Users/<username> or /home/<username> patterns
    text = re.sub(rf'/Users/{re.escape(username)}', '~', text)
    text = re.sub(rf'/home/{re.escape(username)}', '~', text)
    return text


def _path_display(filepath: str, vault_path: str, redact: bool = False) -> str:
    """Convert a file path to an Obsidian-appropriate display."""
    if vault_path and filepath.startswith(vault_path):
        relative = filepath[len(vault_path):].lstrip("/")
        if relative.endswith(".md"):
            relative = relative[:-3]
        return f"[[{relative}]]"

    home = str(Path.home())
    if filepath.startswith(home):
        return f"`~{filepath[len(home):]}`"

    if redact:
        filepath = _redact_text(filepath)

    return f"`{filepath}`"


def generate_filename(
    session: Session,
    template: str = "{date} - {title}.md",
) -> str:
    """Generate a filename for the exported session."""
    # Cascade: created_at → modified_at → source_mtime → "undated"
    dt = session.created_at or session.modified_at
    if dt is None and session.source_mtime:
        dt = datetime.fromtimestamp(session.source_mtime, tz=timezone.utc)
    date_str = dt.strftime("%Y%m%d%H%M") if dt else "undated"

    title = session.title
    # Strip XML/HTML tags that leak from CC system messages
    title = re.sub(r'<[^>]+>', '', title)
    # Strip file paths
    title = re.sub(r'/\S+', '', title)
    # Sanitize for filesystem
    title = re.sub(r'[<>:"/\\|?*\n\r]', "", title)
    # Collapse whitespace
    title = re.sub(r'\s+', ' ', title)
    title = title[:80].strip()
    # Don't end on punctuation fragments
    title = title.rstrip(".,;:!? ")

    filename = template.format(
        date=date_str,
        title=title,
        slug=session.slug or "unknown",
        session_id=session.session_id[:8],
    )

    return filename


def export_session(
    session: Session,
    output_dir: Path,
    vault_path: str = "",
    collapsed_tools: bool = True,
    tool_result_max: int = 500,
    default_tags: list[str] | None = None,
    filename_template: str = "{date} - {title}.md",
    redact_paths: bool = False,
    subagents: list[dict] | None = None,
) -> Path:
    """Export a session to an Obsidian markdown file.

    Returns the path to the written file.
    """
    filename = generate_filename(session, filename_template)
    output_path = output_dir / filename

    content = render_session(
        session,
        vault_path=vault_path,
        collapsed_tools=collapsed_tools,
        tool_result_max=tool_result_max,
        default_tags=default_tags,
        redact_paths=redact_paths,
        subagents=subagents,
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")

    return output_path
