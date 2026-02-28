"""Streaming JSONL parser for Claude Code session files."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ccfolio.models import Session, TokenUsage, ToolCall, ToolResult, Turn
from ccfolio.pricing import calculate_cost, get_model_family


def _clean_first_prompt(text: str) -> str:
    """Strip CC system injection prefixes from a first prompt string."""
    # Remove XML/HTML tags
    clean = re.sub(r'<[^>]+>', '', text).strip()
    # Strip Caveat: prefix blocks (multi-sentence system messages)
    clean = re.sub(
        r'^Caveat:.*?(?:respond|otherwise)\b[^.]*\.', '', clean,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    # Strip DO NOT respond prefix
    clean = re.sub(
        r'^DO NOT respond to this context.*?\.', '', clean,
        flags=re.DOTALL,
    ).strip()
    # Strip "This session is being continued" prefix
    clean = re.sub(
        r'^This session is being continued.*?\.', '', clean,
        flags=re.DOTALL,
    ).strip()
    # Strip "IMPORTANT:" prefix lines
    clean = re.sub(r'^IMPORTANT:.*\n?', '', clean, flags=re.IGNORECASE).strip()
    # Fall back to original if stripping made it empty
    return clean if clean else text.strip()


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO8601 timestamp from CC JSONL."""
    if not ts:
        return None
    try:
        # Handle both Z and +00:00 formats
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def summarize_tool_input(name: str, input_data: dict) -> str:
    """Create a human-readable summary of a tool call's input."""
    if name == "Bash":
        desc = input_data.get("description", "")
        if desc:
            return desc
        cmd = input_data.get("command", "")
        return cmd[:100] + ("..." if len(cmd) > 100 else "")

    if name == "Read":
        path = input_data.get("file_path", "")
        parts = []
        if path:
            parts.append(_short_path(path))
        offset = input_data.get("offset")
        limit = input_data.get("limit")
        if offset or limit:
            parts.append(f"lines {offset or 1}-{(offset or 1) + (limit or 0)}")
        return " ".join(parts)

    if name in ("Write", "MultiEdit"):
        return _short_path(input_data.get("file_path", ""))

    if name == "Edit":
        path = _short_path(input_data.get("file_path", ""))
        old = input_data.get("old_string", "")[:50]
        return f"{path}: \"{old}{'...' if len(input_data.get('old_string', '')) > 50 else ''}\""

    if name == "Glob":
        pattern = input_data.get("pattern", "")
        path = input_data.get("path", "")
        if path:
            return f"{pattern} in {_short_path(path)}"
        return pattern

    if name == "Grep":
        pattern = input_data.get("pattern", "")
        path = input_data.get("path", "")
        if path:
            return f"/{pattern}/ in {_short_path(path)}"
        return f"/{pattern}/"

    if name == "Task":
        return input_data.get("description", input_data.get("prompt", "")[:80])

    if name in ("WebSearch", "WebFetch"):
        return input_data.get("query", input_data.get("url", ""))[:100]

    if name == "TodoWrite":
        todos = input_data.get("todos", [])
        return f"{len(todos)} todos"

    # MCP tools
    if name.startswith("mcp__"):
        parts = name.split("__")
        short_name = parts[-1] if len(parts) > 1 else name
        first_val = next(
            (str(v)[:80] for v in input_data.values() if v),
            "",
        )
        return f"{short_name}: {first_val}" if first_val else short_name

    # Generic fallback
    first_val = next(
        (str(v)[:80] for v in input_data.values() if isinstance(v, str) and v),
        "",
    )
    return first_val or name


def _short_path(path: str) -> str:
    """Shorten a file path for display."""
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def extract_file_paths(turns: list[Turn]) -> dict[str, set[str]]:
    """Extract file paths from tool calls, grouped by operation."""
    paths: dict[str, set[str]] = {
        "read": set(),
        "write": set(),
        "edit": set(),
        "glob": set(),
        "grep": set(),
        "bash": set(),
    }

    bash_path_re = re.compile(r'(?:/[^\s;|&"\'<>]+|~/[^\s;|&"\'<>]+)')

    for turn in turns:
        for tc in turn.tool_calls:
            raw = tc.input_raw
            if tc.name == "Read":
                p = raw.get("file_path", "")
                if p:
                    paths["read"].add(p)
            elif tc.name == "Write":
                p = raw.get("file_path", "")
                if p:
                    paths["write"].add(p)
            elif tc.name == "Edit":
                p = raw.get("file_path", "")
                if p:
                    paths["edit"].add(p)
            elif tc.name == "MultiEdit":
                p = raw.get("file_path", "")
                if p:
                    paths["edit"].add(p)
            elif tc.name == "Glob":
                p = raw.get("path", "")
                if p:
                    paths["glob"].add(p)
            elif tc.name == "Grep":
                p = raw.get("path", "")
                if p:
                    paths["grep"].add(p)
            elif tc.name == "Bash":
                cmd = raw.get("command", "")
                for match in bash_path_re.findall(cmd):
                    # Filter out flags and common false positives
                    if not match.startswith("/-") and "." in match.split("/")[-1]:
                        paths["bash"].add(match)

    return paths


def parse_session_file(
    filepath: Path,
    project_path: str = "",
    project_encoded: str = "",
    include_turns: bool = True,
    sessions_index: dict | None = None,
) -> Session:
    """Parse a single session JSONL file into a Session object.

    Args:
        filepath: Path to the .jsonl file
        project_path: Decoded project path
        project_encoded: Encoded project directory name
        include_turns: Whether to parse individual turns (False for index-only)
        sessions_index: Optional sessions-index.json data for summaries
    """
    session = Session(
        session_id=filepath.stem,
        source_file=str(filepath),
        source_mtime=filepath.stat().st_mtime,
        project_path=project_path,
        project_encoded=project_encoded,
    )

    turns: list[Turn] = []
    model_counter: Counter[str] = Counter()
    tool_counter: Counter[str] = Counter()
    turn_usages: list[tuple[TokenUsage, str]] = []
    first_user_ts: datetime | None = None
    last_ts: datetime | None = None
    total_duration_ms = 0
    sidechain_session_ids: set[str] = set()

    with open(filepath, encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")

            # Track sidechain session IDs (child agents embedded in parent file)
            if entry.get("isSidechain"):
                sidechain_sid = entry.get("sessionId", "")
                if sidechain_sid:
                    sidechain_session_ids.add(sidechain_sid)
                continue

            # Extract session metadata from any entry that has it
            if not session.session_id or session.session_id == filepath.stem:
                sid = entry.get("sessionId", "")
                if sid:
                    session.session_id = sid

            if not session.slug:
                session.slug = entry.get("slug", "")

            if not session.cwd:
                session.cwd = entry.get("cwd", "")

            if not session.git_branch:
                session.git_branch = entry.get("gitBranch") or None

            if not session.cc_version:
                session.cc_version = entry.get("version", "")

            # Track timestamps
            ts = parse_timestamp(entry.get("timestamp"))

            # === custom-title ===
            if entry_type == "custom-title":
                session.custom_title = entry.get("customTitle", "")
                continue

            # === progress / queue-operation / file-history-snapshot ===
            if entry_type in ("progress", "queue-operation", "file-history-snapshot"):
                # Count but don't store - these are noise for conversation
                continue

            # === system ===
            if entry_type == "system":
                subtype = entry.get("subtype", "")
                if subtype == "turn_duration":
                    total_duration_ms += entry.get("durationMs", 0)
                continue

            # === user ===
            if entry_type == "user":
                msg = entry.get("message", {})
                content = msg.get("content", "")

                if not session.permission_mode:
                    session.permission_mode = entry.get("permissionMode", "")

                text = ""
                tool_results: list[ToolResult] = []

                if isinstance(content, str):
                    text = content
                    # Track first user message
                    if not session.first_prompt and text.strip():
                        session.first_prompt = _clean_first_prompt(text)
                elif isinstance(content, list):
                    for block in content:
                        if block.get("type") == "tool_result":
                            result_content = block.get("content", "")
                            if isinstance(result_content, list):
                                # Content can be an array of text blocks
                                result_content = "\n".join(
                                    b.get("text", "") for b in result_content
                                    if isinstance(b, dict)
                                )
                            # Normalize: avoid storing raw Python repr like "[]"
                            content_str = result_content if isinstance(result_content, str) else ""
                            tool_results.append(ToolResult(
                                tool_use_id=block.get("tool_use_id", ""),
                                content=content_str,
                                is_error=block.get("is_error", False),
                            ))
                        elif block.get("type") == "text":
                            text += block.get("text", "")

                if ts and not first_user_ts:
                    first_user_ts = ts
                if ts:
                    last_ts = ts

                session.user_message_count += 1

                if include_turns and (text.strip() or tool_results):
                    turns.append(Turn(
                        uuid=entry.get("uuid", ""),
                        parent_uuid=entry.get("parentUuid"),
                        role="user",
                        timestamp=ts or datetime.now(timezone.utc),
                        text_content=text,
                        tool_results=tool_results,
                    ))

            # === assistant ===
            elif entry_type == "assistant":
                msg = entry.get("message", {})
                content = msg.get("content", [])
                model = msg.get("model", "")

                # Skip synthetic/empty model IDs from streaming artifacts
                if model and model not in ("<synthetic>", ""):
                    model_counter[model] += 1

                # Parse usage
                usage_data = msg.get("usage", {})
                usage = TokenUsage(
                    input_tokens=usage_data.get("input_tokens", 0),
                    output_tokens=usage_data.get("output_tokens", 0),
                    cache_creation_tokens=usage_data.get("cache_creation_input_tokens", 0),
                    cache_read_tokens=usage_data.get("cache_read_input_tokens", 0),
                )
                session.total_usage += usage
                if model:
                    turn_usages.append((usage, model))

                text = ""
                tool_calls: list[ToolCall] = []

                if isinstance(content, list):
                    for block in content:
                        if block.get("type") == "text":
                            text += block.get("text", "")
                        elif block.get("type") == "tool_use":
                            tc_name = block.get("name", "")
                            tc_input = block.get("input", {})
                            tool_calls.append(ToolCall(
                                tool_id=block.get("id", ""),
                                name=tc_name,
                                input_summary=summarize_tool_input(tc_name, tc_input),
                                input_raw=tc_input,
                            ))
                            tool_counter[tc_name] += 1

                if ts:
                    last_ts = ts

                session.assistant_message_count += 1
                session.tool_call_count += len(tool_calls)

                if include_turns and (text.strip() or tool_calls):
                    turns.append(Turn(
                        uuid=entry.get("uuid", ""),
                        parent_uuid=entry.get("parentUuid"),
                        role="assistant",
                        timestamp=ts or datetime.now(timezone.utc),
                        text_content=text,
                        tool_calls=tool_calls,
                        model=model,
                        usage=usage,
                    ))

    # Finalize session metadata
    session.created_at = first_user_ts
    session.modified_at = last_ts
    session.duration_ms = total_duration_ms
    session.tool_calls_by_name = dict(tool_counter)

    # Models
    if model_counter:
        session.models_used = list(model_counter.keys())
        session.primary_model = model_counter.most_common(1)[0][0]

    # Cost
    session.estimated_cost_usd = sum(
        calculate_cost(u, m) for u, m in turn_usages
    )

    # File paths
    if include_turns:
        file_paths = extract_file_paths(turns)
        session.files_read = sorted(file_paths["read"])
        session.files_written = sorted(file_paths["write"])
        session.files_edited = sorted(file_paths["edit"])
        all_files = set()
        for paths in file_paths.values():
            all_files.update(paths)
        session.files_touched = sorted(all_files)
        session.turns = turns

    # Child agent session IDs (from sidechain entries in this file)
    session.child_agent_ids = [
        sid for sid in sidechain_session_ids if sid != session.session_id
    ]

    # Check for subagents (newer format: subagents/ subdirectory)
    subagent_dir = filepath.parent / "subagents"
    if subagent_dir.exists():
        subagent_files = list(subagent_dir.glob("agent-*.jsonl"))
        session.subagent_count = len(subagent_files)
        session.subagent_ids = [f.stem for f in subagent_files]

    # Pull summary from sessions-index if available
    if sessions_index:
        entries = sessions_index.get("entries", [])
        for idx_entry in entries:
            if idx_entry.get("sessionId") == session.session_id:
                if not session.summary:
                    session.summary = idx_entry.get("summary", "")
                if not session.first_prompt:
                    session.first_prompt = idx_entry.get("firstPrompt", "")
                break

    return session


def discover_sessions(claude_home: Path) -> list[dict]:
    """Discover all session files under ~/.claude/projects/.

    Returns list of dicts with keys:
        filepath, project_path, project_encoded, sessions_index
    """
    projects_dir = claude_home / "projects"
    if not projects_dir.exists():
        return []

    sessions = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        project_encoded = project_dir.name
        # Decode project path: -Users-name- -> /Users/name
        project_path = "/" + project_encoded.strip("-").replace("-", "/")

        # Load sessions-index.json if it exists
        index_file = project_dir / "sessions-index.json"
        sessions_index = None
        if index_file.exists():
            try:
                with open(index_file) as f:
                    sessions_index = json.load(f)
            except (json.JSONDecodeError, OSError):
                pass

        for jsonl_file in sorted(project_dir.glob("*.jsonl")):
            # Skip agent files — they are subagent artifacts, not standalone sessions
            if jsonl_file.name.startswith("agent-"):
                continue
            sessions.append({
                "filepath": jsonl_file,
                "project_path": project_path,
                "project_encoded": project_encoded,
                "sessions_index": sessions_index,
            })

    return sessions


def discover_agent_files(claude_home: Path) -> list[dict]:
    """Discover all subagent JSONL files under ~/.claude/projects/.

    Finds agent files in two locations:
    - Project root: agent-*.jsonl (older format)
    - Subagents subdirectory: subagents/agent-*.jsonl (newer format)

    Returns list of dicts with keys:
        filepath, project_path, project_encoded
    """
    projects_dir = claude_home / "projects"
    if not projects_dir.exists():
        return []

    agents = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue

        project_encoded = project_dir.name
        project_path = "/" + project_encoded.strip("-").replace("-", "/")

        # Older format: agent-*.jsonl in project root
        for jsonl_file in sorted(project_dir.glob("agent-*.jsonl")):
            agents.append({
                "filepath": jsonl_file,
                "project_path": project_path,
                "project_encoded": project_encoded,
            })

        # Newer format: subagents/ subdirectory
        subagent_dir = project_dir / "subagents"
        if subagent_dir.exists():
            for jsonl_file in sorted(subagent_dir.glob("agent-*.jsonl")):
                agents.append({
                    "filepath": jsonl_file,
                    "project_path": project_path,
                    "project_encoded": project_encoded,
                })

    return agents
