"""CLI interface for Claude Chronicle."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from chronicle import __version__
from ccfolio.config import Config
from ccfolio.database import Database
from ccfolio.markdown import export_session, render_session
from ccfolio.parser import parse_session_file
from ccfolio.pricing import get_model_family
from ccfolio.sync import sync_agents, sync_sessions

console = Console()


def get_config(ctx: click.Context) -> Config:
    return ctx.obj["config"]


def get_db(ctx: click.Context) -> Database:
    return ctx.obj["db"]


@click.group()
@click.option("--config", "config_path", type=click.Path(exists=False), default=None,
              help="Path to config file")
@click.version_option(version=__version__)
@click.pass_context
def main(ctx: click.Context, config_path: str | None) -> None:
    """Claude Chronicle: Bridge Claude Code conversations into Obsidian."""
    config = Config.load(Path(config_path) if config_path else None)
    config.ensure_dirs()
    db = Database(config.db_path)
    ctx.ensure_object(dict)
    ctx.obj["config"] = config
    ctx.obj["db"] = db

    # Ensure cleanup
    ctx.call_on_close(db.close)


# ── sync ─────────────────────────────────────────────────────────────

@main.command()
@click.option("--full", is_flag=True, help="Re-index all sessions regardless of changes")
@click.option("--project", default=None, help="Only sync sessions from matching project")
@click.pass_context
def sync(ctx: click.Context, full: bool, project: str | None) -> None:
    """Index new and changed sessions from Claude Code."""
    config = get_config(ctx)
    db = get_db(ctx)

    if not config.claude_home.exists():
        console.print(f"[red]Claude home not found: {config.claude_home}[/red]")
        raise SystemExit(1)

    stats = sync_sessions(config, db, full=full, project_filter=project)

    console.print()
    console.print(
        f"[green]Synced:[/green] {stats['new']} new, {stats['updated']} updated, "
        f"{stats['skipped']} unchanged, {stats['errors']} errors "
        f"(of {stats['total']} total)"
    )


# ── update ───────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def update(ctx: click.Context) -> None:
    """Sync new sessions and export any that changed. One command does it all."""
    config = get_config(ctx)
    db = get_db(ctx)

    if not config.claude_home.exists():
        console.print(f"[red]Claude home not found: {config.claude_home}[/red]")
        raise SystemExit(1)

    # Sync sessions
    stats = sync_sessions(config, db, full=False)
    new_or_updated = stats["new"] + stats["updated"]

    if new_or_updated == 0:
        console.print("[dim]Everything up to date.[/dim]")
        return

    console.print(
        f"[green]Synced:[/green] {stats['new']} new, {stats['updated']} updated"
    )

    # Sync agents (links them to parents, appends content to parent FTS)
    agent_stats = sync_agents(config, db, full=False)
    if agent_stats["new"] or agent_stats["updated"]:
        console.print(
            f"[green]Agents:[/green] {agent_stats['new']} new, {agent_stats['updated']} updated"
        )

    # Export changed sessions
    output_dir = config.get_output_path()
    if not output_dir:
        console.print("[dim]No vault configured, skipping export.[/dim]")
        return

    sessions = db.get_sessions_needing_export()
    if not sessions:
        return

    exported = 0
    for record in sessions:
        try:
            _export_one(record, output_dir, config, db)
            db.mark_exported(record["session_id"])
            exported += 1
        except Exception:
            pass

    if exported:
        console.print(f"[green]Exported {exported} sessions[/green] to {output_dir}")


# ── list ─────────────────────────────────────────────────────────────

@main.command("list")
@click.option("--recent", "-n", type=int, default=None, help="Show last N sessions")
@click.option("--project", "-p", default=None, help="Filter by project path")
@click.option("--model", "-m", default=None, help="Filter by model")
@click.option("--favorites", "-f", is_flag=True, help="Only show favorites")
@click.option("--tag", "-t", default=None, help="Filter by tag")
@click.option("--after", default=None, help="Sessions after date (YYYY-MM-DD)")
@click.option("--before", default=None, help="Sessions before date (YYYY-MM-DD)")
@click.option("--sort", "sort_by", default="date",
              type=click.Choice(["date", "cost", "messages", "tokens"]))
@click.pass_context
def list_sessions(
    ctx: click.Context,
    recent: int | None,
    project: str | None,
    model: str | None,
    favorites: bool,
    tag: str | None,
    after: str | None,
    before: str | None,
    sort_by: str,
) -> None:
    """List indexed sessions."""
    db = get_db(ctx)

    sessions = db.list_sessions(
        project=project,
        model=model,
        favorites_only=favorites,
        tag=tag,
        after=after,
        before=before,
        sort_by=sort_by,
        limit=recent,
    )

    if not sessions:
        console.print("[dim]No sessions found. Run 'chronicle sync' first.[/dim]")
        return

    table = Table(
        show_header=True, header_style="bold",
        show_lines=False, pad_edge=False, expand=True,
    )
    table.add_column("#", style="dim", width=4, no_wrap=True)
    table.add_column("", width=1, no_wrap=True)  # favorite star
    table.add_column("Date", width=10, no_wrap=True)
    table.add_column("Title", ratio=1, no_wrap=True, overflow="ellipsis")
    table.add_column("Msgs", justify="right", width=5, no_wrap=True)
    table.add_column("Tools", justify="right", width=5, no_wrap=True)
    table.add_column("Model", width=7, no_wrap=True)
    table.add_column("Cost", justify="right", width=7, no_wrap=True)

    for i, s in enumerate(sessions, 1):
        date = s["created_at"][:10] if s["created_at"] else "—"

        # Title priority: custom title > summary > first prompt > slug
        title = s["custom_title"] or s["summary"] or ""
        if not title:
            prompt = s["first_prompt"] or ""
            # Strip system artifacts and paths from display
            prompt = re.sub(r'<[^>]+>', '', prompt).strip()
            prompt = re.sub(r'/\S+', '', prompt).strip()
            prompt = re.sub(r'\s+', ' ', prompt)
            # Strip CC caveat prefix
            if prompt.startswith("Caveat:"):
                prompt = prompt.split(".", 1)[-1].strip() if "." in prompt else prompt
            title = prompt[:80] if prompt else (s["slug"] or s["session_id"][:8])

        msgs = s["user_message_count"] + s["assistant_message_count"]
        model_fam = get_model_family(s["primary_model"]) if s["primary_model"] else "—"
        cost = f"${s['estimated_cost_usd']:.2f}" if s["estimated_cost_usd"] else "—"
        fav = "[yellow]*[/yellow]" if s["is_favorited"] else ""

        table.add_row(
            str(i),
            fav,
            date,
            title,
            str(msgs),
            str(s["tool_call_count"]),
            model_fam,
            cost,
        )

    console.print(table)
    console.print(f"\n[dim]{len(sessions)} sessions[/dim]")


# ── show ─────────────────────────────────────────────────────────────

@main.command()
@click.argument("session_id")
@click.option("--raw", is_flag=True, help="Show raw database record")
@click.pass_context
def show(ctx: click.Context, session_id: str, raw: bool) -> None:
    """Display a session in the terminal."""
    config = get_config(ctx)
    db = get_db(ctx)

    resolved = db.resolve_session_id(session_id)
    if not resolved:
        console.print(f"[red]Session not found: {session_id}[/red]")
        raise SystemExit(1)

    if raw:
        record = db.get_session(resolved)
        console.print_json(json.dumps(record, indent=2, default=str))
        return

    record = db.get_session(resolved)
    if not record:
        console.print(f"[red]Session not found: {resolved}[/red]")
        raise SystemExit(1)

    # Parse full session with turns for display
    source = record["source_file"]
    if not Path(source).exists():
        console.print(f"[red]Source file missing: {source}[/red]")
        raise SystemExit(1)

    session = parse_session_file(Path(source), include_turns=True)
    session.is_favorited = bool(record["is_favorited"])
    session.tags = json.loads(record["tags_json"])
    session.summary = record["summary"] or session.summary
    session.custom_title = record["custom_title"] or session.custom_title

    # Render as markdown and display
    md = render_session(
        session,
        vault_path=config.obsidian.vault_path,
        collapsed_tools=False,  # Show expanded in terminal
        tool_result_max=config.obsidian.tool_result_max_length,
    )

    console.print(Panel(
        md,
        title=f"[bold]{session.title}[/bold]",
        subtitle=f"[dim]{session.session_id[:8]}[/dim]",
        border_style="blue",
    ))


# ── resume ───────────────────────────────────────────────────────────

@main.command()
@click.argument("session_id")
@click.option("--fork", is_flag=True, help="Create a new session instead of resuming in-place")
@click.pass_context
def resume(ctx: click.Context, session_id: str, fork: bool) -> None:
    """Resume a Claude Code session. Resolves the session and hands off to claude --resume."""
    db = get_db(ctx)
    resolved = db.resolve_session_id(session_id)
    if not resolved:
        console.print(f"[red]Session not found: {session_id}[/red]")
        raise SystemExit(1)

    record = db.get_session(resolved)
    title = record["custom_title"] or record["summary"] or (record["first_prompt"] or "")[:60]
    console.print(f"[green]Resuming:[/green] {title}")
    console.print(f"[dim]{resolved}[/dim]")

    # Close DB before exec replaces the process
    db.close()

    claude_bin = shutil.which("claude")
    if not claude_bin:
        console.print("[red]Error: 'claude' not found in PATH. Is Claude Code installed?[/red]")
        raise SystemExit(1)

    args = ["claude", "--resume", resolved]
    if fork:
        args.append("--fork-session")
    os.execvp("claude", args)


# ── search ───────────────────────────────────────────────────────────

@main.command()
@click.argument("query")
@click.option("--limit", "-n", default=20, help="Max results")
@click.pass_context
def search(ctx: click.Context, query: str, limit: int) -> None:
    """Full-text search across all sessions."""
    db = get_db(ctx)

    results = db.search(query, limit=limit)

    if not results:
        console.print(f"[dim]No results for '{query}'[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Date", width=12)
    table.add_column("Title", max_width=50)
    table.add_column("Snippet", max_width=60)
    table.add_column("Cost", justify="right", width=8)

    for i, r in enumerate(results, 1):
        date = r["created_at"][:10] if r["created_at"] else "—"
        title = r["custom_title"] or r["summary"] or (r["first_prompt"] or "")[:40]
        snippet = r.get("snippet", "")[:80] if r.get("snippet") else ""
        # Clean up FTS highlight markers for rich
        snippet = snippet.replace(">>>", "[bold yellow]").replace("<<<", "[/bold yellow]")
        cost = f"${r['estimated_cost_usd']:.2f}" if r["estimated_cost_usd"] else "—"

        table.add_row(str(i), date, title, snippet, cost)

    console.print(table)
    console.print(f"\n[dim]{len(results)} results[/dim]")


# ── export ───────────────────────────────────────────────────────────

@main.command()
@click.argument("session_id", required=False)
@click.option("--all", "export_all", is_flag=True, help="Export all un-exported sessions")
@click.option("--favorites", is_flag=True, help="Export only favorites")
@click.option("--force", is_flag=True, help="Re-export even if already exported")
@click.option("--output", "-o", type=click.Path(), default=None, help="Output directory")
@click.option("--after", default=None, help="Export sessions after date")
@click.option("--redact-paths", is_flag=True, help="Redact home directory from file paths in export")
@click.pass_context
def export(
    ctx: click.Context,
    session_id: str | None,
    export_all: bool,
    favorites: bool,
    force: bool,
    output: str | None,
    after: str | None,
    redact_paths: bool = False,
) -> None:
    """Export sessions to Obsidian markdown."""
    config = get_config(ctx)
    db = get_db(ctx)

    # Determine output directory
    if output:
        output_dir = Path(output)
    else:
        output_dir = config.get_output_path()
        if not output_dir:
            console.print(
                "[red]No vault path configured. Use --output or run 'chronicle config init'[/red]"
            )
            raise SystemExit(1)

    output_dir.mkdir(parents=True, exist_ok=True)

    if session_id and not export_all:
        # Single session export
        resolved = db.resolve_session_id(session_id)
        if not resolved:
            console.print(f"[red]Session not found: {session_id}[/red]")
            raise SystemExit(1)

        record = db.get_session(resolved)
        _export_one(record, output_dir, config, db, redact=redact_paths)
        return

    # Batch export
    if force:
        # Force: re-export everything matching filters
        sessions = db.list_sessions(
            favorites_only=favorites,
            after=after,
            sort_by="date",
        )
    else:
        # Smart: only export sessions that were indexed after last export
        sessions = db.get_sessions_needing_export()
        if favorites:
            sessions = [s for s in sessions if s["is_favorited"]]
        if after:
            sessions = [s for s in sessions if s.get("created_at", "") >= after]

    if not sessions:
        console.print("[dim]No sessions need exporting.[/dim]")
        return

    exported = 0
    errors = 0
    for record in sessions:
        try:
            _export_one(record, output_dir, config, db, redact=redact_paths)
            db.mark_exported(record["session_id"])
            exported += 1
        except Exception as e:
            errors += 1
            console.print(f"[red]Error exporting {record['session_id'][:8]}: {e}[/red]")

    console.print(
        f"[green]Exported {exported} sessions[/green]"
        + (f", {errors} errors" if errors else "")
        + f" to {output_dir}"
    )


def _export_one(record: dict, output_dir: Path, config: Config, db: Database, redact: bool = False) -> None:
    """Export a single session from its database record."""
    source = record["source_file"]
    if not Path(source).exists():
        console.print(f"[yellow]Source missing: {source}[/yellow]")
        return

    # Get sessions-index for this project
    sessions_index = None
    idx_file = Path(source).parent / "sessions-index.json"
    if idx_file.exists():
        try:
            sessions_index = json.loads(idx_file.read_text())
        except (json.JSONDecodeError, OSError):
            pass

    session = parse_session_file(
        Path(source),
        project_path=record["project_path"],
        project_encoded=record["project_encoded"],
        include_turns=True,
        sessions_index=sessions_index,
    )
    session.is_favorited = bool(record["is_favorited"])
    session.tags = json.loads(record["tags_json"])
    session.summary = record["summary"] or session.summary
    session.custom_title = record["custom_title"] or session.custom_title

    subagents = db.get_agents_for_session(record["session_id"])

    path = export_session(
        session,
        output_dir=output_dir,
        vault_path=config.obsidian.vault_path,
        collapsed_tools=config.obsidian.tool_calls_collapsed,
        tool_result_max=config.obsidian.tool_result_max_length,
        default_tags=config.obsidian.default_tags,
        filename_template=config.obsidian.filename_template,
        redact_paths=redact,
        subagents=subagents or None,
    )
    console.print(f"  [green]Exported:[/green] {path.name}")


# ── rename ───────────────────────────────────────────────────────────

@main.command()
@click.argument("session_id")
@click.argument("title")
@click.pass_context
def rename(ctx: click.Context, session_id: str, title: str) -> None:
    """Set a custom title for a session."""
    db = get_db(ctx)
    resolved = db.resolve_session_id(session_id)
    if not resolved:
        console.print(f"[red]Session not found: {session_id}[/red]")
        raise SystemExit(1)

    db.set_custom_title(resolved, title)
    console.print(f"[green]Renamed {resolved[:8]}:[/green] {title}")


# ── fav ──────────────────────────────────────────────────────────────

@main.command()
@click.argument("session_id")
@click.pass_context
def fav(ctx: click.Context, session_id: str) -> None:
    """Toggle favorite on a session."""
    db = get_db(ctx)
    resolved = db.resolve_session_id(session_id)
    if not resolved:
        console.print(f"[red]Session not found: {session_id}[/red]")
        raise SystemExit(1)

    new_state = db.toggle_favorite(resolved)
    icon = "[yellow]*[/yellow]" if new_state else ""
    console.print(f"{'Favorited' if new_state else 'Unfavorited'} {resolved[:8]} {icon}")


# ── tag / untag ──────────────────────────────────────────────────────

@main.command()
@click.argument("session_id")
@click.argument("tags", nargs=-1, required=True)
@click.pass_context
def tag(ctx: click.Context, session_id: str, tags: tuple[str, ...]) -> None:
    """Add tags to a session."""
    db = get_db(ctx)
    resolved = db.resolve_session_id(session_id)
    if not resolved:
        console.print(f"[red]Session not found: {session_id}[/red]")
        raise SystemExit(1)

    updated = db.add_tags(resolved, list(tags))
    console.print(f"Tags for {resolved[:8]}: {', '.join(updated)}")


@main.command()
@click.argument("session_id")
@click.argument("tags", nargs=-1, required=True)
@click.pass_context
def untag(ctx: click.Context, session_id: str, tags: tuple[str, ...]) -> None:
    """Remove tags from a session."""
    db = get_db(ctx)
    resolved = db.resolve_session_id(session_id)
    if not resolved:
        console.print(f"[red]Session not found: {session_id}[/red]")
        raise SystemExit(1)

    updated = db.remove_tags(resolved, list(tags))
    console.print(f"Tags for {resolved[:8]}: {', '.join(updated) or '(none)'}")


# ── cost ─────────────────────────────────────────────────────────────

@main.command()
@click.option("--daily", "group", flag_value="daily", default=True)
@click.option("--monthly", "group", flag_value="monthly")
@click.option("--model", "group", flag_value="model")
@click.option("--project", "group", flag_value="project")
@click.option("--after", default=None, help="After date")
@click.option("--before", default=None, help="Before date")
@click.pass_context
def cost(ctx: click.Context, group: str, after: str | None, before: str | None) -> None:
    """Show cost breakdown."""
    config = get_config(ctx)
    db = get_db(ctx)

    rows = db.get_cost_summary(group_by=group, after=after, before=before)

    if not rows:
        console.print("[dim]No cost data found.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Period" if group != "model" else "Model", width=20)
    table.add_column("Sessions", justify="right", width=8)
    table.add_column("Input Tokens", justify="right", width=14)
    table.add_column("Output Tokens", justify="right", width=14)
    table.add_column("Cost (API)", justify="right", width=10)

    total_cost = 0.0
    total_sessions = 0
    total_in = 0
    total_out = 0
    # Filter out empty rows and sort by cost descending for model view
    filtered = [r for r in rows if r["total_cost"]]
    if group == "model":
        filtered.sort(key=lambda r: r["total_cost"] or 0, reverse=True)

    for r in filtered:
        table.add_row(
            str(r["period"]),
            str(r["session_count"]),
            f"{r['input_tokens']:,}" if r["input_tokens"] else "0",
            f"{r['output_tokens']:,}" if r["output_tokens"] else "0",
            f"${r['total_cost']:.2f}" if r["total_cost"] else "$0.00",
        )
        total_cost += r["total_cost"] or 0
        total_sessions += r["session_count"] or 0
        total_in += r["input_tokens"] or 0
        total_out += r["output_tokens"] or 0

    # Total row
    table.add_section()
    table.add_row(
        "[bold]Total[/bold]",
        f"[bold]{total_sessions}[/bold]",
        f"[bold]{total_in:,}[/bold]",
        f"[bold]{total_out:,}[/bold]",
        f"[bold]${total_cost:.2f}[/bold]",
    )

    console.print(table)

    if config.billing_mode == "max":
        console.print("\n[dim]Running on Max subscription ($0 per-token cost)[/dim]")
    elif config.billing_mode == "both":
        console.print(
            f"\n[dim]Would-be API cost: ${total_cost:.2f} "
            "(Max subscription: $0 actual)[/dim]"
        )


# ── stats ────────────────────────────────────────────────────────────

@main.command()
@click.option("--tools", is_flag=True, help="Show tool usage breakdown")
@click.option("--models", is_flag=True, help="Show model usage breakdown")
@click.pass_context
def stats(ctx: click.Context, tools: bool, models: bool) -> None:
    """Show usage statistics."""
    db = get_db(ctx)
    s = db.get_stats()

    if not s["total_sessions"]:
        console.print("[dim]No sessions indexed. Run 'chronicle sync' first.[/dim]")
        return

    console.print(Panel(
        f"[bold]Sessions:[/bold] {s['total_sessions']}\n"
        f"[bold]Messages:[/bold] {(s['total_user_messages'] or 0) + (s['total_assistant_messages'] or 0):,} "
        f"({s['total_user_messages'] or 0:,} user, {s['total_assistant_messages'] or 0:,} assistant)\n"
        f"[bold]Tool calls:[/bold] {s['total_tool_calls'] or 0:,}\n"
        f"[bold]Tokens:[/bold] {(s['total_input_tokens'] or 0) + (s['total_output_tokens'] or 0):,} "
        f"({s['total_input_tokens'] or 0:,} in, {s['total_output_tokens'] or 0:,} out)\n"
        f"[bold]Estimated cost:[/bold] ${s['total_cost'] or 0:.2f}\n"
        f"[bold]Favorites:[/bold] {s['favorite_count'] or 0}\n"
        f"[bold]Period:[/bold] {(s['earliest_session'] or '')[:10]} to {(s['latest_session'] or '')[:10]}",
        title="Chronicle Stats",
        border_style="blue",
    ))

    if tools:
        console.print()
        tool_stats = db.get_tool_stats()
        table = Table(title="Tool Usage", show_header=True, header_style="bold")
        table.add_column("Tool", width=20)
        table.add_column("Uses", justify="right", width=10)
        for name, count in tool_stats[:20]:
            table.add_row(name, f"{count:,}")
        console.print(table)


# ── files ────────────────────────────────────────────────────────────

@main.command()
@click.argument("file_path")
@click.pass_context
def files(ctx: click.Context, file_path: str) -> None:
    """Find sessions that touched a file."""
    db = get_db(ctx)

    sessions = db.find_sessions_for_file(file_path)

    if not sessions:
        console.print(f"[dim]No sessions found touching '{file_path}'[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("#", style="dim", width=4)
    table.add_column("Date", width=12)
    table.add_column("Title", max_width=50)
    table.add_column("Model", width=8)
    table.add_column("Cost", justify="right", width=8)

    for i, s in enumerate(sessions, 1):
        date = s["created_at"][:10] if s["created_at"] else "—"
        title = s["custom_title"] or s["summary"] or (s["first_prompt"] or "")[:50]
        model_fam = get_model_family(s["primary_model"]) if s["primary_model"] else "—"
        cost = f"${s['estimated_cost_usd']:.2f}" if s["estimated_cost_usd"] else "—"
        table.add_row(str(i), date, title, model_fam, cost)

    console.print(table)
    console.print(f"\n[dim]{len(sessions)} sessions touched '{file_path}'[/dim]")


# ── config ───────────────────────────────────────────────────────────

@main.group("config")
def config_cmd() -> None:
    """Manage configuration."""
    pass


@config_cmd.command("show")
@click.pass_context
def config_show(ctx: click.Context) -> None:
    """Show current configuration."""
    config = get_config(ctx)
    console.print(Panel(
        f"[bold]Config file:[/bold] {config.config_file}\n"
        f"[bold]Claude home:[/bold] {config.claude_home}\n"
        f"[bold]Database:[/bold] {config.db_path}\n"
        f"[bold]Billing mode:[/bold] {config.billing_mode}\n"
        f"[bold]Vault path:[/bold] {config.obsidian.vault_path or '(not set)'}\n"
        f"[bold]Output dir:[/bold] {config.obsidian.output_dir}\n"
        f"[bold]Filename template:[/bold] {config.obsidian.filename_template}\n"
        f"[bold]Default tags:[/bold] {', '.join(config.obsidian.default_tags)}",
        title="Chronicle Config",
        border_style="blue",
    ))


@config_cmd.command("init")
@click.pass_context
def config_init(ctx: click.Context) -> None:
    """Interactive configuration setup."""
    config = get_config(ctx)

    console.print("[bold]Claude Chronicle Setup[/bold]")
    console.print()

    # Detect Claude home
    claude_home = config.claude_home
    if claude_home.exists():
        console.print(f"[green]Found Claude Code at:[/green] {claude_home}")
    else:
        claude_home_str = click.prompt("Claude Code home directory", default=str(claude_home))
        claude_home = Path(claude_home_str).expanduser()

    # Vault path
    vault_path = click.prompt(
        "Obsidian vault path",
        default=config.obsidian.vault_path or "",
    )

    # Output directory
    output_dir = click.prompt(
        "Output directory (relative to vault)",
        default=config.obsidian.output_dir,
    )

    # Billing mode
    billing = click.prompt(
        "Billing mode (api/max/both)",
        default=config.billing_mode,
        type=click.Choice(["api", "max", "both"]),
    )

    # Write config
    config_content = f"""[general]
claude_home = "{claude_home}"
db_path = "{config.db_path}"
billing_mode = "{billing}"

[obsidian]
vault_path = "{vault_path}"
output_dir = "{output_dir}"
filename_template = "{{date}} - {{title}}.md"
path_display = "wikilink"
tool_result_max_length = 500
tool_calls_collapsed = true
default_tags = ["Claude-Session"]
subagent_display = "summary"
"""

    config.config_file.parent.mkdir(parents=True, exist_ok=True)
    config.config_file.write_text(config_content)
    console.print(f"\n[green]Config written to:[/green] {config.config_file}")


# ── mcp ─────────────────────────────────────────────────────────────

@main.command()
@click.pass_context
def mcp(ctx: click.Context) -> None:
    """Start Chronicle as an MCP server (stdio transport)."""
    try:
        from ccfolio.mcp_server import create_server
    except ImportError:
        console.print(
            "[red]MCP dependencies not installed.[/red]\n"
            "Run: pip install claude-chronicle[mcp]"
        )
        raise SystemExit(1)

    config = get_config(ctx)
    server = create_server(config)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
