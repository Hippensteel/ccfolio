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


@dataclass
class Config:
    claude_home: Path = field(default_factory=lambda: DEFAULT_CLAUDE_HOME)
    db_path: Path = field(default_factory=lambda: DEFAULT_DB_PATH)
    billing_mode: str = "both"  # api | max | both
    obsidian: ObsidianConfig = field(default_factory=ObsidianConfig)
    config_file: Path = field(default_factory=lambda: DEFAULT_CONFIG_FILE)

    @classmethod
    def load(cls, config_path: Path | None = None) -> Config:
        """Load config from TOML file, environment variables, and defaults."""
        config = cls()

        # Environment variable overrides (checked first, lowest priority after file)
        env_claude_home = os.environ.get("CCFOLIO_CLAUDE_HOME")
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
                    "default_tags", "subagent_display",
                ]:
                    if key in obs:
                        setattr(config.obsidian, key, obs[key])

            config.config_file = path

        # Environment overrides take precedence
        if env_claude_home:
            config.claude_home = Path(env_claude_home).expanduser()
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
