"""Auto-generate titles for sessions that weren't manually renamed."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ccfolio.models import Session

# Current user's home directory name (for filtering from topics)
_USERNAME = Path.home().name

# Directories that are too generic to be useful as topics
SKIP_DIRS = {
    "users", "home", "src", "lib", "bin", "var", "tmp", "opt",
    "etc", "data", "test", "tests", "docs", "build", "dist",
    ".venv", "venv", "node_modules", ".git", "__pycache__",
    "documents",
}

# Common parent dirs to skip through to find the real project name
# NOTE: project paths from CC encode hyphens as path separators,
# so "claude-sandbox" becomes "claude/sandbox". We match individual
# components here, not full directory names.
CONTAINER_DIRS = {
    "claude-sandbox", "projects", "vaults",
    "claude", "sandbox",  # decoded fragments of "claude-sandbox"
}

# Stop words for prompt keyword extraction
STOP_WORDS = {
    "a", "an", "the", "and", "or", "but", "in", "on", "at", "to", "for",
    "of", "with", "by", "from", "is", "it", "its", "that", "this", "was",
    "are", "be", "been", "has", "have", "had", "do", "does", "did", "will",
    "can", "could", "would", "should", "may", "might", "shall",
    "i", "me", "my", "we", "our", "you", "your", "he", "she", "they",
    "not", "no", "so", "if", "then", "than", "just", "also", "about",
    "all", "any", "some", "up", "out", "how", "what", "when", "where",
    "which", "who", "why", "hi", "hey", "hello", "claude", "please",
    "want", "need", "let", "lets", "make", "like", "going", "get",
    "got", "know", "think", "see", "look", "try", "use", "using",
    "im", "ive", "dont", "were", "thats", "whats", "heres",
    "run", "running", "install", "installed", "update", "updated",
    "keep", "getting", "error", "errors", "work", "working",
    "code", "file", "files", "command", "thing", "things",
    "actually", "right", "here", "there", "still", "really",
    "something", "everything", "anything", "nothing", "first",
    "new", "old", "last", "next", "good", "well", "done",
    "trying", "tried", "says", "said", "saying", "called",
    "caveat", "messages", "below", "generated", "user", "while",
    "switched", "from", "native", "installer",
    "background", "according", "quarter", "units", "sold",
    "price", "average", "https", "docs", "respond", "these",
    "otherwise", "context", "disk", "keeps", "filling",
    "every", "much", "more", "most", "many", "each",
    "only", "same", "other", "another", "been", "being",
    "users", "folder", "offloaded",
    "super", "god", "safe", "now", "source",
}


def generate_auto_title(session: Session) -> str:
    """Generate a topic-based title from session content.

    Format: "Topic1 + Topic2 + Topic3" (max ~60 chars, max 4 topics).
    Returns empty string if no meaningful topics can be extracted.
    """
    topics: list[str] = []

    # 1. Project name as anchor topic
    project = _project_topic(session.project_path)
    if project:
        topics.append(project)

    # 2. File-based topics from most-touched directories
    # Weight writes/edits higher than reads
    weighted_files = []
    for f in session.files_written:
        weighted_files.extend([f, f])  # 2x weight
    for f in session.files_edited:
        weighted_files.extend([f, f])  # 2x weight
    for f in session.files_read:
        weighted_files.append(f)

    dir_topics = _file_topics(weighted_files)
    for t in dir_topics:
        if not _is_duplicate(t, topics):
            topics.append(t)
            if len(topics) >= 4:
                break

    # 3. Extract keywords from first prompt if we still need topics
    if len(topics) < 2 and session.first_prompt:
        prompt_kws = _prompt_topics(session.first_prompt)
        for kw in prompt_kws:
            if not _is_duplicate(kw, topics):
                topics.append(kw)
                if len(topics) >= 3:
                    break

    if not topics:
        return ""

    return _format_title(topics)


def _project_topic(project_path: str) -> str:
    """Extract a clean topic name from the project directory path."""
    if not project_path:
        return ""

    home = str(Path.home())
    # If the project path is just the home directory, no useful topic
    if project_path.rstrip("/") == home.rstrip("/"):
        return ""

    parts = Path(project_path).parts
    # Walk from the end to find the first meaningful directory
    for part in reversed(parts):
        lower = part.lower().replace("-", "").replace("_", "")
        if lower in SKIP_DIRS or lower == _USERNAME.lower() or part == "/":
            continue
        if part.lower() in {d.lower() for d in CONTAINER_DIRS}:
            continue
        return _clean_dir_name(part)

    return ""


def _file_topics(file_paths: list[str]) -> list[str]:
    """Extract topic names from file paths based on directory clustering."""
    if not file_paths:
        return []

    home = str(Path.home())
    dir_counts: Counter[str] = Counter()

    for fp in file_paths:
        # Normalize path
        if fp.startswith(home):
            fp = fp[len(home):]
        fp = fp.lstrip("/")

        parts = Path(fp).parts
        # Find meaningful directory components (skip containers and generic dirs)
        for part in parts:
            lower = part.lower().replace("-", "").replace("_", "")
            if lower in SKIP_DIRS:
                continue
            if part.lower().replace("-", "") in {d.replace("-", "") for d in CONTAINER_DIRS}:
                continue
            if lower in {
                _USERNAME.lower(), ".claude", ".config", "claude", "sandbox",
                "users", "library", "application", "support", "google",
                "private", "desktop", "downloads",
            }:
                continue
            # Skip filenames (has extension)
            if "." in part and part != ".zshrc":
                continue
            dir_counts[part] += 1

    if not dir_counts:
        return []

    # Return cleaned names, most common first
    seen = set()
    topics = []
    for dirname, _count in dir_counts.most_common(6):
        clean = _clean_dir_name(dirname)
        if clean.lower() not in seen:
            seen.add(clean.lower())
            topics.append(clean)

    return topics


def _prompt_topics(first_prompt: str) -> list[str]:
    """Extract meaningful keywords from the first prompt."""
    if not first_prompt:
        return []

    # Clean up
    text = re.sub(r'<[^>]+>', '', first_prompt)  # strip XML tags
    # Strip CC system caveat/resume prefixes (can be multi-sentence)
    text = re.sub(r'^Caveat:.*?(?:respond|otherwise)\b[^.]*\.', '', text, flags=re.DOTALL | re.IGNORECASE).strip()
    text = re.sub(r'^DO NOT respond to this context.*?\.', '', text, flags=re.DOTALL).strip()
    # Strip any remaining leading system noise
    text = re.sub(r'^This session is being continued.*?\.', '', text, flags=re.DOTALL).strip()
    text = re.sub(r'[^\w\s-]', ' ', text)  # strip punctuation
    text = re.sub(r'\s+', ' ', text).strip()

    words = text.split()
    if not words:
        return []

    # Look for multi-word capitalized phrases (proper nouns, project names)
    # Require at least 2 words OR a single word that is 5+ chars to avoid junk
    phrases = []
    i = 0
    while i < len(words):
        word = words[i]
        lower = word.lower()
        # Skip: not capitalized, in stop words, all-numeric, single-char
        if not word[0:1].isupper() or lower in STOP_WORDS or word.isdigit() or len(word) < 2:
            i += 1
            continue
        # Skip words that start with a digit (e.g. "001")
        if word[0].isdigit():
            i += 1
            continue

        phrase_parts = [word]
        j = i + 1
        while (j < len(words)
               and words[j][0:1].isupper()
               and words[j].lower() not in STOP_WORDS
               and not words[j][0].isdigit()):
            phrase_parts.append(words[j])
            j += 1

        if len(phrase_parts) >= 2:
            # Multi-word phrase: always accept (reasonable signal)
            phrases.append(" ".join(phrase_parts))
        elif len(word) >= 5 and not word.isdigit():
            # Single capitalized word: only if 5+ chars (filters "He", "Hex", "Run")
            phrases.append(word)
        i = j

    # Then single meaningful words (lowercase or mixed, 4+ chars)
    singles = []
    for w in words:
        lower = w.lower()
        # Skip stop words, short words, pure numbers, words starting with digit
        if lower in STOP_WORDS or len(lower) <= 3 or lower.isdigit() or w[0].isdigit():
            continue
        singles.append(w.title() if w.islower() else w)

    # Deduplicate: phrases first, then singles
    seen = set()
    result = []
    for p in phrases + singles:
        key = p.lower()
        if key not in seen:
            seen.add(key)
            result.append(p)

    return result[:5]


def _clean_dir_name(dirname: str) -> str:
    """Convert a directory name into a readable topic name."""
    # Special cases for common project directories
    name = dirname

    # Replace separators with spaces
    name = name.replace("-", " ").replace("_", " ")

    # Title case, preserving acronyms (all-caps words stay caps)
    parts = name.split()
    cleaned = []
    for p in parts:
        if p.isupper() and len(p) > 1:
            cleaned.append(p)  # Keep acronyms
        else:
            cleaned.append(p.capitalize())
    name = " ".join(cleaned)

    # Common abbreviation fixups
    name = re.sub(r'\bMcp\b', 'MCP', name)
    name = re.sub(r'\bCli\b', 'CLI', name)
    name = re.sub(r'\bApi\b', 'API', name)
    name = re.sub(r'\bArxiv\b', 'arXiv', name)
    name = re.sub(r'\bUi\b', 'UI', name)
    name = re.sub(r'\bDb\b', 'DB', name)
    name = re.sub(r'\bSql\b', 'SQL', name)
    name = re.sub(r'\bLlm\b', 'LLM', name)
    name = re.sub(r'\bN8n\b', 'n8n', name)
    name = re.sub(r'\bZshrc\b', 'zshrc', name)
    name = re.sub(r'\bObsidian\b', 'Obsidian', name)

    return name


def _is_duplicate(candidate: str, existing: list[str]) -> bool:
    """Check if a topic is already represented in the list."""
    c_lower = candidate.lower().replace(" ", "")
    for e in existing:
        e_lower = e.lower().replace(" ", "")
        # Exact match or substring containment
        if c_lower == e_lower or c_lower in e_lower or e_lower in c_lower:
            return True
    return False


def _format_title(topics: list[str]) -> str:
    """Join topics into a title, respecting the ~60 char limit."""
    for limit in (4, 3, 2, 1):
        title = " + ".join(topics[:limit])
        if len(title) <= 60:
            return title
    # If even one topic is too long, truncate it
    return topics[0][:60] if topics else ""
