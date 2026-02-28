"""Data models for Claude Chronicle."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0

    def __iadd__(self, other: TokenUsage) -> TokenUsage:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_creation_tokens += other.cache_creation_tokens
        self.cache_read_tokens += other.cache_read_tokens
        return self


@dataclass
class ToolCall:
    tool_id: str
    name: str
    input_summary: str
    input_raw: dict = field(default_factory=dict, repr=False)


@dataclass
class ToolResult:
    tool_use_id: str
    content: str
    is_error: bool = False


@dataclass
class Turn:
    uuid: str
    parent_uuid: str | None
    role: str  # "user" | "assistant"
    timestamp: datetime
    text_content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)
    model: str | None = None
    usage: TokenUsage | None = None


@dataclass
class Session:
    session_id: str
    slug: str = ""
    custom_title: str = ""
    project_path: str = ""
    project_encoded: str = ""
    cwd: str = ""
    git_branch: str | None = None

    # Timing
    created_at: datetime | None = None
    modified_at: datetime | None = None
    duration_ms: int = 0

    # Content
    first_prompt: str = ""
    summary: str = ""

    # Counts
    user_message_count: int = 0
    assistant_message_count: int = 0
    tool_call_count: int = 0
    tool_calls_by_name: dict[str, int] = field(default_factory=dict)

    # Models
    models_used: list[str] = field(default_factory=list)
    primary_model: str = ""

    # Tokens & cost
    total_usage: TokenUsage = field(default_factory=TokenUsage)
    estimated_cost_usd: float = 0.0

    # Files
    files_read: list[str] = field(default_factory=list)
    files_written: list[str] = field(default_factory=list)
    files_edited: list[str] = field(default_factory=list)
    files_touched: list[str] = field(default_factory=list)

    # Subagents
    subagent_count: int = 0
    subagent_ids: list[str] = field(default_factory=list)
    child_agent_ids: list[str] = field(default_factory=list)  # internal UUIDs of child agents

    # Metadata
    cc_version: str = ""
    permission_mode: str = ""
    is_favorited: bool = False
    tags: list[str] = field(default_factory=list)

    # Source
    source_file: str = ""
    source_mtime: float = 0.0

    # Conversation
    turns: list[Turn] = field(default_factory=list)

    @property
    def title(self) -> str:
        """Best available title for this session."""
        if self.custom_title:
            return self.custom_title
        if self.summary:
            return self.summary
        if self.first_prompt:
            prompt = self.first_prompt[:80]
            if len(self.first_prompt) > 80:
                prompt += "..."
            return prompt
        return self.slug or self.session_id[:8]

    @property
    def duration_display(self) -> str:
        """Human-readable duration."""
        if not self.duration_ms:
            # Fall back to timestamp diff
            if self.created_at and self.modified_at:
                diff = (self.modified_at - self.created_at).total_seconds()
            else:
                return ""
        else:
            diff = self.duration_ms / 1000

        if diff < 60:
            return f"{int(diff)}s"
        elif diff < 3600:
            return f"{int(diff / 60)}m"
        else:
            hours = int(diff / 3600)
            mins = int((diff % 3600) / 60)
            return f"{hours}h {mins}m" if mins else f"{hours}h"
