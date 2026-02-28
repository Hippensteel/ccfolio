"""Sync Claude Code sessions into the Chronicle database."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, MofNCompleteColumn

from ccfolio.autotitle import generate_auto_title
from ccfolio.config import Config
from ccfolio.database import Database
from ccfolio.parser import discover_agent_files, discover_sessions, parse_session_file

console = Console()


def sync_sessions(
    config: Config,
    db: Database,
    full: bool = False,
    project_filter: str | None = None,
) -> dict:
    """Sync sessions from ~/.claude/ into the database.

    Args:
        config: Chronicle configuration
        db: Database instance
        full: If True, re-index all sessions regardless of mtime
        project_filter: Only sync sessions from matching project path

    Returns:
        Dict with counts: new, updated, skipped, errors
    """
    stats = {"new": 0, "updated": 0, "skipped": 0, "errors": 0, "total": 0}

    session_infos = discover_sessions(config.claude_home)
    stats["total"] = len(session_infos)

    if project_filter:
        session_infos = [
            s for s in session_infos
            if project_filter in s["project_path"]
        ]

    if not session_infos:
        console.print("[dim]No session files found.[/dim]")
        return stats

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Syncing sessions...", total=len(session_infos))

        for info in session_infos:
            filepath = info["filepath"]
            session_id = filepath.stem

            try:
                current_mtime = filepath.stat().st_mtime
                stored_mtime = db.get_session_mtime(session_id)

                if not full and stored_mtime is not None:
                    if current_mtime <= stored_mtime:
                        stats["skipped"] += 1
                        progress.update(task, advance=1)
                        continue

                session = parse_session_file(
                    filepath=filepath,
                    project_path=info["project_path"],
                    project_encoded=info["project_encoded"],
                    include_turns=True,
                    sessions_index=info.get("sessions_index"),
                )

                # Auto-generate title for sessions without one
                if not session.custom_title and not session.summary:
                    session.summary = generate_auto_title(session)

                # Build content text for FTS indexing
                content_parts = []
                for turn in session.turns:
                    if turn.text_content.strip():
                        content_parts.append(turn.text_content.strip())
                content_text = "\n\n".join(content_parts)

                # Truncate content for FTS (50K chars max)
                if len(content_text) > 50000:
                    content_text = content_text[:50000]

                db.upsert_session(session, content_text)

                if stored_mtime is None:
                    stats["new"] += 1
                else:
                    stats["updated"] += 1

            except Exception as e:
                stats["errors"] += 1
                console.print(f"[red]Error parsing {filepath.name}: {e}[/red]")

            progress.update(task, advance=1)

    return stats


def sync_agents(
    config: Config,
    db: Database,
    full: bool = False,
) -> dict:
    """Sync subagent files and link them to parent sessions.

    Args:
        config: Chronicle configuration
        db: Database instance
        full: If True, re-index all agents regardless of mtime

    Returns:
        Dict with counts: new, updated, skipped, errors
    """
    from pathlib import Path

    stats = {"new": 0, "updated": 0, "skipped": 0, "errors": 0, "total": 0}

    agent_infos = discover_agent_files(config.claude_home)
    stats["total"] = len(agent_infos)

    if not agent_infos:
        return stats

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Syncing agents...", total=len(agent_infos))

        for info in agent_infos:
            filepath = info["filepath"]
            agent_id = filepath.stem  # e.g. "agent-a592e5c"

            try:
                current_mtime = filepath.stat().st_mtime
                stored_mtime = db.get_agent_mtime(agent_id)

                if not full and stored_mtime is not None:
                    if current_mtime <= stored_mtime:
                        stats["skipped"] += 1
                        progress.update(task, advance=1)
                        continue

                # Parse the agent file (reuses same parser as regular sessions)
                agent_session = parse_session_file(
                    filepath=filepath,
                    project_path=info["project_path"],
                    project_encoded=info["project_encoded"],
                    include_turns=True,
                )

                # Build searchable content text
                content_parts = []
                for turn in agent_session.turns:
                    if turn.text_content.strip():
                        content_parts.append(turn.text_content.strip())
                content_text = "\n\n".join(content_parts)
                if len(content_text) > 50000:
                    content_text = content_text[:50000]

                # Find parent session by matching agent's session ID to child_agent_ids
                agent_session_id = agent_session.session_id
                parent_session_id = db.get_parent_session_id_for_agent(agent_session_id)

                # Extract task_description and subagent_type from parent's Task tool calls
                task_description = ""
                subagent_type = ""
                if parent_session_id:
                    parent_source = db.conn.execute(
                        "SELECT source_file FROM sessions WHERE session_id = ?",
                        (parent_session_id,),
                    ).fetchone()
                    if parent_source and parent_source["source_file"]:
                        parent_session = parse_session_file(
                            filepath=Path(parent_source["source_file"]),
                            include_turns=True,
                        )
                        for turn in parent_session.turns:
                            for tc in turn.tool_calls:
                                if tc.name == "Task":
                                    task_description = tc.input_raw.get(
                                        "description", tc.input_raw.get("prompt", "")
                                    )[:500]
                                    subagent_type = tc.input_raw.get("subagent_type", "")
                                    break
                            if task_description:
                                break

                db.upsert_agent(
                    agent_id=agent_id,
                    agent_session_id=agent_session_id,
                    parent_session_id=parent_session_id,
                    task_description=task_description,
                    subagent_type=subagent_type,
                    first_prompt=agent_session.first_prompt,
                    content_text=content_text,
                    source_file=str(filepath),
                    source_mtime=current_mtime,
                )

                # Append agent content to parent's FTS index so it's searchable
                if parent_session_id and content_text:
                    db.append_agent_content_to_fts(parent_session_id, content_text)

                if stored_mtime is None:
                    stats["new"] += 1
                else:
                    stats["updated"] += 1

            except Exception as e:
                stats["errors"] += 1
                console.print(f"[red]Error parsing agent {filepath.name}: {e}[/red]")

            progress.update(task, advance=1)

    return stats
