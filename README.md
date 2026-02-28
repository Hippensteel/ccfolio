# ccfolio

Index, search, and archive your Claude Code conversations. Tracks cost, exports to Obsidian, and lets you resume past sessions without losing context.

## What It Does

Claude Code stores every conversation as JSONL files in `~/.claude/projects/`. ccfolio indexes them into a local SQLite database and gives you:

- **Full-text search** across all conversations, including subagent content
- **Cost tracking** — daily, monthly, by model, by project
- **Obsidian export** with Dataview/Bases-compatible frontmatter
- **Auto-generated titles** from project context and first prompt keywords
- **Session resume** — hand off directly to `claude --resume` by index, slug, or ID prefix
- **MCP server** — search past sessions from within an active Claude Code conversation
- **Subagent linking** — agent artifacts linked to parent sessions, not indexed as noise

## Install

```bash
pip install ccfolio

# With MCP server support
pip install ccfolio[mcp]
```

Then run setup:

```bash
ccfolio config init
```

This sets your Obsidian vault path and configures the auto-sync hook.

## Auto-Sync

Add this to your `~/.zshrc` to auto-sync after every Claude Code session:

```bash
claude() {
    command claude "$@"
    ccfolio update --quiet
}
```

After that, ccfolio runs silently every time you exit a `claude` session.

## Commands

### Browse

```bash
ccfolio list                     # All sessions, newest first
ccfolio list --recent 20         # Last 20
ccfolio list --sort cost         # Most expensive first
ccfolio list --sort messages     # Most messages first
ccfolio list --favorites         # Only favorites
ccfolio list --model opus        # Filter by model
ccfolio list --after 2026-02-01  # After a date
```

### Search

```bash
ccfolio search "tailscale"
ccfolio search "hypothesis engine"
ccfolio search "CLAUDE.md"
```

Searches user messages, assistant responses, summaries, file paths, and subagent content. Supports partial words — `ccfolio search "folio"` finds sessions containing "ccfolio".

```bash
# Find sessions that touched a specific file
ccfolio files "CLAUDE.md"
ccfolio files "fetch_daily.py"
```

### View

```bash
ccfolio show #1                  # By list index
ccfolio show giggly-pondering    # By slug (partial match works)
ccfolio show 0c560d              # By ID prefix
ccfolio show #1 --raw            # Raw JSON record
```

### Resume

```bash
ccfolio resume #1                # Resume most recent session
ccfolio resume giggly-pondering  # Resume by slug
ccfolio resume #3 --fork         # Fork into a new session
```

Resolves the session and hands off to `claude --resume`. Never loses context.

### Cost

```bash
ccfolio cost                     # Daily breakdown
ccfolio cost --monthly           # Monthly breakdown
ccfolio cost --model             # By model
ccfolio cost --project           # By project directory
ccfolio cost --after 2026-02-01  # Date range
```

Shows estimated API cost alongside your usage. Useful for understanding where tokens go even on a Max subscription.

### Stats

```bash
ccfolio stats                    # Overview: sessions, messages, tokens, cost
ccfolio stats --tools            # Tool usage breakdown
```

### Organize

```bash
ccfolio fav #3                   # Toggle favorite
ccfolio tag #3 infrastructure    # Add tags
ccfolio untag #3 infrastructure  # Remove tags
ccfolio rename #3 "My Title"     # Set custom title
```

### Export to Obsidian

```bash
ccfolio export #3                # Export single session
ccfolio export --all             # Export all un-exported sessions
ccfolio export --all --force     # Re-export everything
ccfolio export --all --redact-paths  # Strip username from file paths
```

Exports happen automatically via `ccfolio update`. Manual export available for one-offs.

### MCP Server

ccfolio can run as an MCP server, letting Claude search past sessions from within an active conversation.

Add to `~/.claude.json` under your project path:

```json
{
  "projects": {
    "/Users/yourname": {
      "mcpServers": {
        "ccfolio": {
          "command": "/path/to/.venv/bin/ccfolio",
          "args": ["mcp"]
        }
      }
    }
  }
}
```

Available tools: `search_sessions`, `list_recent_sessions`, `find_sessions_for_file`, `get_session_details`, `get_cost_summary`.

## Session ID Shortcuts

You never need to type a full UUID:

| Format | Example |
|--------|---------|
| Index from last `list` | `#1`, `#12` |
| Slug (partial) | `giggly`, `giggly-pondering` |
| ID prefix | `0c560d` |

## Obsidian Output

Exported sessions land in your configured vault path with frontmatter:

```yaml
type: Claude-Session
session_id: "0c560d4a-..."
slug: giggly-pondering-gem
date: 2026-02-14
model: claude-opus-4-6
messages: 159
tool_calls: 50
cost_usd: 11.95
favorite: false
tags: [Claude-Session, Opus]
```

Tool calls render as collapsible callouts. File paths to vault notes become wikilinks. Sessions that used subagents get a linked Subagents section.

## Auto-Titles

Sessions without a custom `/rename` title get automatic topic-based names during sync. ccfolio extracts topics from the project directory, most-touched file paths, and first prompt keywords.

Format: `Topic1 + Topic2 + Topic3` (max 4 topics). No LLM calls, no API cost.

## Requirements

- Python 3.10+
- Claude Code (`claude` CLI)
- Obsidian (optional, for vault export)

## Configuration

```bash
ccfolio config show    # View current settings
ccfolio config init    # Interactive setup
```

Config file: `~/.config/ccfolio/config.toml`
Database: `~/.config/ccfolio/ccfolio.db`

## License

MIT
