"""Parser for Gemini CLI session JSON files."""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from ccfolio.models import Session, TokenUsage, ToolCall, ToolResult, Turn
from ccfolio.pricing import calculate_cost

# Map Gemini CLI tool names (often MCP format) to ccfolio tool names
def _map_tool_name(name: str) -> str:
    # Example MCP tool: `mcp__google-workspace__people.getMe`
    # Or local tools like `run_shell_command`
    if name == "run_shell_command":
        return "Bash"
    if name == "read_file":
        return "Read"
    if name == "write_file":
        return "Write"
    if name == "replace" or name == "edit_file":
        return "Edit"
    if name == "glob" or name == "list_directory":
        return "Glob"
    if name == "grep_search":
        return "Grep"
    
    # Strip mcp prefix if present, but keep original for context
    if name.startswith("mcp__"):
        parts = name.split("__")
        if len(parts) >= 3:
            return parts[2]
        return name
    
    return name

def _summarize_gemini_tool(name: str, args: dict | str) -> str:
    """Create a human-readable summary of a Gemini tool call."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except (json.JSONDecodeError, TypeError):
            return str(args)[:100]

    if not isinstance(args, dict):
        return str(args)[:100]

    mapped = _map_tool_name(name)
    if mapped == "Bash":
        cmd = args.get("command", args.get("cmd", ""))
        return cmd[:100] + ("..." if len(cmd) > 100 else "")
    
    if mapped in ("Read", "Write", "Edit"):
        path = args.get("file_path", args.get("path", ""))
        return _short_path(path) if path else mapped
        
    if mapped == "Glob":
        path = args.get("dir_path", args.get("path", ""))
        return _short_path(path) if path else mapped

    if mapped == "Grep":
        pattern = args.get("pattern", "")
        path = args.get("dir_path", "")
        if path:
            return f"/{pattern}/ in {_short_path(path)}"
        return f"/{pattern}/"
        
    first_val = next(
        (str(v)[:80] for v in args.values() if v and isinstance(v, (str, int, bool, float))), ""
    )
    return f"{mapped}: {first_val}" if first_val else mapped

def _short_path(path: str) -> str:
    home = str(Path.home())
    if path.startswith(home):
        return "~" + path[len(home):]
    return path

def _extract_file_paths_from_gemini(turns: list[Turn]) -> dict[str, set[str]]:
    paths: dict[str, set[str]] = {
        "read": set(), "write": set(), "edit": set(),
        "glob": set(), "grep": set(), "bash": set(),
    }
    
    import re
    bash_path_re = re.compile(r'(?:/[^\s;|&"\'<>]+|~/[^\s;|&"\'<>]+)')

    for turn in turns:
        for tc in turn.tool_calls:
            raw = tc.input_raw
            mapped = _map_tool_name(tc.name)

            if mapped == "Read":
                p = raw.get("file_path", raw.get("path", ""))
                if p: paths["read"].add(p)
            elif mapped == "Write":
                p = raw.get("file_path", raw.get("path", ""))
                if p: paths["write"].add(p)
            elif mapped == "Edit":
                p = raw.get("file_path", raw.get("path", ""))
                if p: paths["edit"].add(p)
            elif mapped == "Glob":
                p = raw.get("dir_path", raw.get("path", ""))
                if p: paths["glob"].add(p)
            elif mapped == "Grep":
                p = raw.get("dir_path", "")
                if p: paths["grep"].add(p)
            elif mapped == "Bash":
                cmd = raw.get("command", raw.get("cmd", ""))
                for match in bash_path_re.findall(cmd):
                    if not match.startswith("/-") and "." in match.split("/")[-1]:
                        paths["bash"].add(match)

    return paths

def parse_timestamp(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        ts = ts.replace("Z", "+00:00")
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None

def parse_gemini_session_file(
    filepath: Path,
    include_turns: bool = True,
) -> Session:
    """Parse a single Gemini CLI JSON session file into a Session object."""
    with open(filepath, encoding="utf-8", errors="replace") as f:
        data = json.load(f)

    session_id = data.get("sessionId", filepath.stem)
    # Project name comes from the parent directory (~/.gemini/tmp/<project>/chats/<file>),
    # not the JSON's `projectHash`. The hash is an opaque internal Google identifier;
    # the directory name is the human-meaningful project ("mainframe", "hypothesis-engine").
    project_dir = filepath.parent.parent if filepath.parent.name == "chats" else filepath.parent
    session = Session(
        session_id=session_id,
        source_file=str(filepath),
        source_mtime=filepath.stat().st_mtime,
        project_path=str(project_dir),
    )
    
    first_user_ts = parse_timestamp(data.get("startTime"))
    last_ts = parse_timestamp(data.get("lastUpdated"))
    session.created_at = first_user_ts
    session.modified_at = last_ts

    turns: list[Turn] = []
    model_counter: Counter[str] = Counter()
    tool_counter: Counter[str] = Counter()
    
    messages = data.get("messages", [])
    
    for msg in messages:
        msg_type = msg.get("type", "")
        ts = parse_timestamp(msg.get("timestamp")) or datetime.now(timezone.utc)
        msg_id = msg.get("id", "")
        
        if msg_type == "user":
            content = msg.get("content", [])
            text_parts = []
            for item in content:
                if "text" in item:
                    text_parts.append(item["text"])
            
            msg_text = "\n".join(text_parts)
            session.user_message_count += 1
            
            if not session.first_prompt and msg_text.strip():
                session.first_prompt = msg_text.strip()[:500]
                
            if include_turns and msg_text.strip():
                turns.append(Turn(
                    uuid=msg_id,
                    parent_uuid=None,
                    role="user",
                    timestamp=ts,
                    text_content=msg_text,
                ))
                
        elif msg_type == "gemini":
            session.assistant_message_count += 1
            
            content_text = msg.get("content", "")
            if isinstance(content_text, list):
                # Just in case Gemini content becomes a list
                text_parts = []
                for item in content_text:
                    if isinstance(item, dict) and "text" in item:
                        text_parts.append(item["text"])
                content_text = "\n".join(text_parts)
            
            model = msg.get("model", "gemini-3-pro")
            model_counter[model] += 1
            
            tokens = msg.get("tokens", {})
            usage = TokenUsage(
                input_tokens=tokens.get("input", 0),
                output_tokens=tokens.get("output", 0),
                cache_read_tokens=tokens.get("cached", 0),
            )
            session.total_usage += usage
            # Cost calc
            session.estimated_cost_usd += calculate_cost(usage, model)
            
            tool_calls_data = msg.get("toolCalls", [])
            tool_calls = []
            tool_results = []
            
            for tc_data in tool_calls_data:
                tc_id = tc_data.get("id", "")
                tc_name = tc_data.get("name", "")
                tc_args = tc_data.get("args", {})
                
                mapped_name = _map_tool_name(tc_name)
                tc = ToolCall(
                    tool_id=tc_id,
                    name=mapped_name,
                    input_summary=_summarize_gemini_tool(tc_name, tc_args),
                    input_raw=tc_args,
                )
                tool_calls.append(tc)
                tool_counter[mapped_name] += 1
                session.tool_call_count += 1
                
                # Build synthetic tool result
                tc_result_disp = tc_data.get("resultDisplay", "")
                if not tc_result_disp and "result" in tc_data:
                     # try to grab it from raw result
                     tc_result_disp = str(tc_data["result"])
                     
                tool_results.append(ToolResult(
                    tool_use_id=tc_id,
                    content=str(tc_result_disp)[:2000],
                    is_error=tc_data.get("status") != "success"
                ))

            if include_turns:
                # Assistant turn with text and tool calls
                turns.append(Turn(
                    uuid=msg_id,
                    parent_uuid=None,
                    role="assistant",
                    timestamp=ts,
                    text_content=str(content_text),
                    tool_calls=tool_calls,
                    model=model,
                    usage=usage,
                ))
                
                # Synthetic user turn for the tool results if any
                if tool_results:
                    turns.append(Turn(
                        uuid="",
                        parent_uuid=msg_id,
                        role="user",
                        timestamp=ts,
                        tool_results=tool_results,
                    ))

    session.tool_calls_by_name = dict(tool_counter)
    if model_counter:
        session.models_used = list(model_counter.keys())
        session.primary_model = model_counter.most_common(1)[0][0]

    if include_turns:
        file_paths = _extract_file_paths_from_gemini(turns)
        session.files_read = sorted(file_paths["read"])
        session.files_written = sorted(file_paths["write"])
        session.files_edited = sorted(file_paths["edit"])
        all_files = set()
        for paths in file_paths.values():
            all_files.update(paths)
        session.files_touched = sorted(all_files)
        session.turns = turns

    return session

def discover_gemini_sessions(gemini_home: Path) -> list[dict]:
    """Discover all Gemini CLI session JSON files under ~/.gemini/tmp/<hash>/chats/.

    Returns list of dicts with keys:
        filepath, project_path, project_encoded
    """
    tmp_dir = gemini_home / "tmp"
    if not tmp_dir.exists():
        return []

    sessions = []
    # Find all JSON files in chats directories
    for json_file in tmp_dir.glob("*/chats/*.json"):
        if not json_file.name.startswith("session-"):
            continue
            
        sessions.append({
            "filepath": json_file,
            "project_path": "", # Not easily determined without reverse hashing
            "project_encoded": "",
        })

    return sessions
