"""Parser for Codex CLI (OpenAI) session JSONL files."""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ccfolio.models import Session, TokenUsage, ToolCall, ToolResult, Turn
from ccfolio.pricing import calculate_cost


# Map Codex tool names to normalized ccfolio tool names
CODEX_TOOL_MAP = {
    "exec_command": "Bash",
    "apply_patch": "Edit",
    "apply_diff": "Edit",
    "read_file": "Read",
    "write_file": "Write",
    "list_directory": "Glob",
    "search_files": "Grep",
    "search_replace": "Edit",
}


def _map_tool_name(codex_name: str) -> str:
    """Map a Codex tool name to its ccfolio equivalent."""
    return CODEX_TOOL_MAP.get(codex_name, codex_name)


def _summarize_codex_tool(name: str, arguments: dict | str) -> str:
    """Create a human-readable summary of a Codex tool call."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except (json.JSONDecodeError, TypeError):
            return str(arguments)[:100]

    if not isinstance(arguments, dict):
        return str(arguments)[:100]

    if name == "exec_command":
        cmd = arguments.get("cmd", arguments.get("command", ""))
        return cmd[:100] + ("..." if len(cmd) > 100 else "")

    if name in ("apply_patch", "apply_diff", "search_replace"):
        path = arguments.get("file_path", arguments.get("path", ""))
        return _short_path(path) if path else name

    if name == "read_file":
        return _short_path(arguments.get("file_path", arguments.get("path", "")))

    if name == "write_file":
        return _short_path(arguments.get("file_path", arguments.get("path", "")))

    if name == "list_directory":
        return _short_path(arguments.get("path", arguments.get("dir", "")))

    if name == "search_files":
        pattern = arguments.get("pattern", arguments.get("query", ""))
        path = arguments.get("path", "")
        if path:
            return f"/{pattern}/ in {_short_path(path)}"
        return f"/{pattern}/"

    # MCP tools from Codex
    if name.startswith("mcp__") or name.startswith("mcp_"):
        first_val = next(
            (str(v)[:80] for v in arguments.values() if v), ""
        )
        return f"{name}: {first_val}" if first_val else name

    # Generic fallback
    first_val = next(
        (str(v)[:80] for v in arguments.values() if isinstance(v, str) and v), ""
    )
    return first_val or name


def _short_path(path: str) -> str:
    """Shorten a file path for display."""
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path


def _extract_file_paths_from_codex(turns: list[Turn]) -> dict[str, set[str]]:
    """Extract file paths from Codex tool calls."""
    paths: dict[str, set[str]] = {
        "read": set(), "write": set(), "edit": set(),
        "glob": set(), "grep": set(), "bash": set(),
    }

    bash_path_re = re.compile(r'(?:/[^\s;|&"\'<>]+|~/[^\s;|&"\'<>]+)')

    for turn in turns:
        for tc in turn.tool_calls:
            raw = tc.input_raw
            mapped = _map_tool_name(tc.name)

            if mapped == "Read":
                p = raw.get("file_path", raw.get("path", ""))
                if p:
                    paths["read"].add(p)
            elif mapped == "Write":
                p = raw.get("file_path", raw.get("path", ""))
                if p:
                    paths["write"].add(p)
            elif mapped == "Edit":
                p = raw.get("file_path", raw.get("path", ""))
                if p:
                    paths["edit"].add(p)
            elif mapped == "Glob":
                p = raw.get("path", raw.get("dir", ""))
                if p:
                    paths["glob"].add(p)
            elif mapped == "Grep":
                p = raw.get("path", "")
                if p:
                    paths["grep"].add(p)
            elif mapped == "Bash":
                cmd = raw.get("cmd", raw.get("command", ""))
                for match in bash_path_re.findall(cmd):
                    if not match.startswith("/-") and "." in match.split("/")[-1]:
                        paths["bash"].add(match)

    return paths


def parse_timestamp(ts: str | None) -> datetime | None:
    """Parse ISO8601 timestamp from Codex JSONL."""
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def parse_codex_session_file(
    filepath: Path,
    include_turns: bool = True,
) -> Session:
    """Parse a single Codex CLI JSONL file into a Session object.

    Codex JSONL entry types:
    - session_meta: Session metadata (id, cwd, model, cli_version)
    - response_item: Messages (user/developer/assistant), function_call, function_call_output
    - event_msg: Events (user_message, agent_message, token_count, task_started, task_complete)
    - turn_context: Context injections
    """
    # Extract session ID from filename: rollout-{timestamp}-{uuid}.jsonl
    filename = filepath.stem
    parts = filename.split("-", 1)
    file_uuid = filename  # fallback

    session = Session(
        session_id=file_uuid,
        source_file=str(filepath),
        source_mtime=filepath.stat().st_mtime,
    )

    turns: list[Turn] = []
    model_counter: Counter[str] = Counter()
    tool_counter: Counter[str] = Counter()
    turn_usages: list[tuple[TokenUsage, str]] = []
    first_user_ts: datetime | None = None
    last_ts: datetime | None = None
    session_model = ""

    # Track pending function calls to pair with outputs
    pending_calls: dict[str, ToolCall] = {}

    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue

            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue

            entry_type = entry.get("type", "")
            ts = parse_timestamp(entry.get("timestamp"))

            # === session_meta ===
            if entry_type == "session_meta":
                payload = entry.get("payload", {})
                sid = payload.get("id", "")
                if sid:
                    session.session_id = sid
                session.cwd = payload.get("cwd", "")
                session.cc_version = payload.get("cli_version", "")
                # Derive project_path from cwd
                if session.cwd:
                    session.project_path = session.cwd
                # Try to extract model from base_instructions text as fallback
                base = payload.get("base_instructions") or {}
                base_text = base.get("text", "") if isinstance(base, dict) else ""
                if base_text and not session_model:
                    import re as _re
                    m = _re.search(r'You are (GPT-[\d.]+|o[\d][\w-]*)', base_text, _re.IGNORECASE)
                    if m:
                        session_model = m.group(1).lower()
                continue

            # === event_msg ===
            if entry_type == "event_msg":
                payload = entry.get("payload", {})
                event_type = payload.get("type", "")

                if event_type == "user_message":
                    msg_text = payload.get("message", "")
                    if ts and not first_user_ts:
                        first_user_ts = ts
                    if ts:
                        last_ts = ts
                    session.user_message_count += 1

                    if not session.first_prompt and msg_text.strip():
                        session.first_prompt = msg_text.strip()[:500]

                    if include_turns and msg_text.strip():
                        turns.append(Turn(
                            uuid="",
                            parent_uuid=None,
                            role="user",
                            timestamp=ts or datetime.now(timezone.utc),
                            text_content=msg_text,
                        ))

                elif event_type == "agent_message":
                    msg_text = payload.get("message", "")
                    if ts:
                        last_ts = ts
                    session.assistant_message_count += 1

                    if include_turns and msg_text.strip():
                        turns.append(Turn(
                            uuid="",
                            parent_uuid=None,
                            role="assistant",
                            timestamp=ts or datetime.now(timezone.utc),
                            text_content=msg_text,
                            model=session_model,
                        ))

                elif event_type == "agent_reasoning":
                    # Reasoning traces - skip for now (not stored in CC either)
                    pass

                elif event_type == "token_count":
                    info = payload.get("info") or {}
                    total_usage = info.get("total_token_usage") or {}
                    last_usage = info.get("last_token_usage") or {}

                    # Use last_token_usage for incremental cost calc
                    usage = TokenUsage(
                        input_tokens=last_usage.get("input_tokens", 0),
                        output_tokens=last_usage.get("output_tokens", 0),
                        cache_read_tokens=last_usage.get("cached_input_tokens", 0),
                        cache_creation_tokens=0,
                    )
                    # Add reasoning tokens to output tokens for cost purposes
                    usage.output_tokens += last_usage.get("reasoning_output_tokens", 0)

                    model_id = session_model or "unknown"
                    if model_id and model_id != "unknown":
                        model_counter[model_id] += 1
                    turn_usages.append((usage, model_id))

                elif event_type == "task_started":
                    # Could extract model_context_window if needed
                    pass

                elif event_type == "task_complete":
                    pass

                continue

            # === response_item ===
            if entry_type == "response_item":
                payload = entry.get("payload", {})
                item_type = payload.get("type", "")

                if item_type == "function_call":
                    call_name = payload.get("name", "")
                    call_id = payload.get("call_id", "")
                    arguments_raw = payload.get("arguments", "{}")

                    try:
                        args_dict = json.loads(arguments_raw) if isinstance(arguments_raw, str) else arguments_raw
                    except (json.JSONDecodeError, TypeError):
                        args_dict = {"raw": arguments_raw}

                    mapped_name = _map_tool_name(call_name)
                    tc = ToolCall(
                        tool_id=call_id,
                        name=mapped_name,
                        input_summary=_summarize_codex_tool(call_name, args_dict),
                        input_raw=args_dict,
                    )
                    pending_calls[call_id] = tc
                    tool_counter[mapped_name] += 1
                    session.tool_call_count += 1

                    if include_turns:
                        turns.append(Turn(
                            uuid=call_id,
                            parent_uuid=None,
                            role="assistant",
                            timestamp=ts or datetime.now(timezone.utc),
                            tool_calls=[tc],
                            model=session_model,
                        ))

                elif item_type == "function_call_output":
                    call_id = payload.get("call_id", "")
                    output = payload.get("output", "")
                    if isinstance(output, list):
                        output = "\n".join(
                            item.get("text", str(item)) if isinstance(item, dict) else str(item)
                            for item in output
                        )

                    if include_turns:
                        turns.append(Turn(
                            uuid="",
                            parent_uuid=None,
                            role="user",
                            timestamp=ts or datetime.now(timezone.utc),
                            tool_results=[ToolResult(
                                tool_use_id=call_id,
                                content=str(output)[:2000],
                                is_error=False,
                            )],
                        ))

                elif item_type == "message":
                    # developer and user role response_items are context injections,
                    # not actual user messages. Skip them for first_prompt.
                    pass

                elif item_type == "reasoning":
                    # Model reasoning traces - skip
                    pass

                elif item_type == "custom_tool_call":
                    # MCP tool calls
                    call_name = payload.get("name", "")
                    call_id = payload.get("call_id", payload.get("id", ""))
                    args = payload.get("arguments", {})
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except (json.JSONDecodeError, TypeError):
                            args = {"raw": args}

                    tc = ToolCall(
                        tool_id=call_id,
                        name=call_name,
                        input_summary=_summarize_codex_tool(call_name, args),
                        input_raw=args if isinstance(args, dict) else {},
                    )
                    tool_counter[call_name] += 1
                    session.tool_call_count += 1

                    if include_turns:
                        turns.append(Turn(
                            uuid=call_id,
                            parent_uuid=None,
                            role="assistant",
                            timestamp=ts or datetime.now(timezone.utc),
                            tool_calls=[tc],
                            model=session_model,
                        ))

                elif item_type == "custom_tool_call_output":
                    call_id = payload.get("call_id", payload.get("id", ""))
                    output = payload.get("output", "")

                    if include_turns:
                        turns.append(Turn(
                            uuid="",
                            parent_uuid=None,
                            role="user",
                            timestamp=ts or datetime.now(timezone.utc),
                            tool_results=[ToolResult(
                                tool_use_id=call_id,
                                content=str(output)[:2000],
                                is_error=False,
                            )],
                        ))

                if ts:
                    last_ts = ts
                continue

            # === turn_context ===
            if entry_type == "turn_context":
                payload = entry.get("payload", {})
                # Extract model from turn_context (most reliable source)
                ctx_model = payload.get("model", "")
                if ctx_model and not session_model:
                    session_model = ctx_model
                continue

    # Finalize session metadata
    session.created_at = first_user_ts
    session.modified_at = last_ts
    session.tool_calls_by_name = dict(tool_counter)

    # Models
    if model_counter:
        session.models_used = list(model_counter.keys())
        session.primary_model = model_counter.most_common(1)[0][0]
    elif session_model:
        session.models_used = [session_model]
        session.primary_model = session_model

    # Cost - use cumulative approach since token_count events report running totals
    # We stored per-event last_token_usage, so sum those
    session.estimated_cost_usd = sum(
        calculate_cost(u, m) for u, m in turn_usages
    )

    # Accumulate total usage from turn_usages
    for usage, _ in turn_usages:
        session.total_usage += usage

    # File paths
    if include_turns:
        file_paths = _extract_file_paths_from_codex(turns)
        session.files_read = sorted(file_paths["read"])
        session.files_written = sorted(file_paths["write"])
        session.files_edited = sorted(file_paths["edit"])
        all_files = set()
        for paths in file_paths.values():
            all_files.update(paths)
        session.files_touched = sorted(all_files)
        session.turns = turns

    return session


def discover_codex_sessions(codex_home: Path) -> list[dict]:
    """Discover all Codex CLI session files under ~/.codex/sessions/.

    Codex organizes sessions as:
        sessions/YYYY/MM/DD/rollout-{timestamp}-{uuid}.jsonl

    Returns list of dicts with keys:
        filepath, project_path, project_encoded
    """
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.exists():
        return []

    sessions = []
    for jsonl_file in sorted(sessions_dir.rglob("*.jsonl")):
        sessions.append({
            "filepath": jsonl_file,
            "project_path": "",  # Will be populated during parse from cwd
            "project_encoded": "",
        })

    return sessions
