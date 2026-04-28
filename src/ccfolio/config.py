"""Configuration management for ccfolio."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]


DEFAULT_CONFIG_DIR = Path.home() / ".config" / "ccfolio"
DEFAULT_CONFIG_FILE = DEFAULT_CONFIG_DIR / "config.toml"
DEFAULT_DB_PATH = DEFAULT_CONFIG_DIR / "ccfolio.db"
DEFAULT_CLAUDE_HOME = Path.home() / ".claude"
DEFAULT_CODEX_HOME = Path.home() / ".codex"
DEFAULT_GEMINI_HOME = Path.home() / ".gemini"


@dataclass
class ObsidianConfig:
    vault_path: str = ""
    output_dir: str = "Reference/Claude Sessions"
    filename_template: str = "{date} - {title}.md"
    path_display: str = "wikilink"  # wikilink | relative | absolute
    tool_result_max_length: int = 500
    tool_calls_collapsed: bool = True
    default_tags: list[str] = field(default_factory=lambda: ["Claude-Session"])
    subagent_display: str = "summary"  # inline | linked | summary
    # When False, ccfolio still indexes sessions in the DB (so cost/list/search
    # queries work) but does not auto-export markdown to the vault. Single-
    # session export by ID (`ccfolio export <id>`) still works. Use this when
    # you want explicit opt-in capture via a save-session skill instead of
    # bulk capture-and-filter.
    auto_export: bool = True


@dataclass
class ExportConfig:
    exclude_projects: list[str] = field(default_factory=list)


@dataclass
class FilterConfig:
    """Filter sessions out of vault export.

    Three OR-combined signals — a session passes (gets exported) if it meets
    ANY of these thresholds. Sessions that fail all three are filtered.

    - `min_user_turns`: minimum human-typed user turns. Multi-turn conversations
      are real chats. Background agents typically have 1 turn.
    - `min_first_prompt_chars`: minimum length of the first user prompt.
      Substantive single-shot work (long prompt → autonomous → exit) typically
      has a long first prompt; agent triggers are short.
    - `min_cost_usd`: minimum estimated cost. Real work tends to cost more
      than ping-style agent runs.

    A threshold of 0 disables that signal. If all three are 0, no filtering.

    Filtering is applied at export time, not sync time — sessions are still
    written to the DB so cost/usage queries see them, just not exported as
    markdown to the vault.
    """
    min_user_turns: int = 0
    min_first_prompt_chars: int = 0
    min_cost_usd: float = 0.0


@dataclass
class SourcesConfig:
    claude_code: bool = True
    codex: bool = True
    gemini: bool = True


@dataclass
class Config:
    claude_home: Path = field(default_factory=lambda: DEFAULT_CLAUDE_HOME)
    codex_home: Path = field(default_factory=lambda: DEFAULT_CODEX_HOME)
    gemini_home: Path = field(default_factory=lambda: DEFAULT_GEMINI_HOME)
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    billing_mode: str = "both"  # api | max | both
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    export: ExportConfig = field(default_factory=ExportConfig)
    filter: FilterConfig = field(default_factory=FilterConfig)
    sources: SourcesConfig = field(default_factory=SourcesConfig)
    config_file: Path = field(default_factory=lambda: DEFAULT_CONFIG_FILE)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        """Load config from TOML file, environment variables, and defaults."""
        config = cls()

        # Environment variable overrides (checked first, lowest priority after file)
        env_claude_home = os.environ.get("CCFOLIO_CLAUDE_HOME")
        env_codex_home = os.environ.get("CCFOLIO_CODEX_HOME")
        env_gemini_home = os.environ.get("CCFOLIO_GEMINI_HOME")
        env_vault = os.environ.get("CCFOLIO_VAULT_PATH")
        env_db = os.environ.get("CCFOLIO_DB_PATH")

        # Try to load config file
        path = config_path or DEFAULT_CONFIG_FILE
        if path.exists():
            with open(path, "rb") as f:
                data = tomllib.load(f)

            general = data.get("general", {})
            if "claude_home" in general:
                config.claude_home = Path(general["claude_home"]).expanduser()
            if "codex_home" in general:
                config.codex_home = Path(general["codex_home"]).expanduser()
            if "gemini_home" in general:
                config.gemini_home = Path(general["gemini_home"]).expanduser()
            if "db_path" in general:
                config.db_path = Path(general["db_path"]).expanduser()
            if "billing_mode" in general:
                config.billing_mode = general["billing_mode"]

            obs = data.get("obsidian", {})
            if obs:
                if "vault_path" in obs:
                    config.obsidian.vault_path = str(Path(obs["vault_path"]).expanduser())
                for key in [
                    "output_dir", "filename_template", "path_display",
                    "tool_result_max_length", "tool_calls_collapsed",
                    "default_tags", "subagent_display", "auto_export",
                ]:
                    if key in obs:
                        setattr(config.obsidian, key, obs[key])

            exp = data.get("export", {})
            if exp:
                if "exclude_projects" in exp:
                    config.export.exclude_projects = exp["exclude_projects"]

            filt = data.get("filter", {})
            if filt:
                if "min_user_turns" in filt:
                    config.filter.min_user_turns = int(filt["min_user_turns"])
                if "min_first_prompt_chars" in filt:
                    config.filter.min_first_prompt_chars = int(filt["min_first_prompt_chars"])
                if "min_cost_usd" in filt:
                    config.filter.min_cost_usd = float(filt["min_cost_usd"])

            sources = data.get("sources", {})
            if sources:
                if "claude_code" in sources:
                    config.sources.claude_code = bool(sources["claude_code"])
                if "codex" in sources:
                    config.sources.codex = bool(sources["codex"])
                if "gemini" in sources:
                    config.sources.gemini = bool(sources["gemini"])

            config.config_file = path

        # Environment overrides take precedence
        if env_claude_home:
            config.claude_home = Path(env_claude_home).expanduser()
        if env_codex_home:
            config.codex_home = Path(env_codex_home).expanduser()
        if env_gemini_home:
            config.gemini_home = Path(env_gemini_home).expanduser()
        if env_vault:
            config.obsidian.vault_path = str(Path(env_vault).expanduser())
        if env_db:
            config.db_path = Path(env_db).expanduser()

        return config

    def ensure_dirs(self) -> None:
        """Create config and database directories if needed."""
        self.config_file.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def projects_dir(self) -> Path:
        return self.claude_home / "projects"

    @property
    def history_file(self) -> Path:
        return self.claude_home / "history.jsonl"

    def get_output_path(self) -> Path | None:
        """Get the full output directory path for Obsidian export."""
        if not self.obsidian.vault_path:
            return None
        return Path(self.obsidian.vault_path) / self.obsidian.output_dir
