#!/usr/bin/env python3
"""Antigravity CLI (agy) HUD — inspired by claude-hud.

Renders a multi-line real-time statusline for Antigravity CLI:
  Line 1: [Model ◑ high] │ project git:(branch*) │ ⏱ 1h 35m
  Line 2: 上下文 ████░░░░░░ 18% │ Gemini 5h: 92% (4h 12m) | 7d: 88% (5d 3h) │ 3P 5h: 75% (2h 40m) | 7d: 81% (6d 1h)
  Line 3: 1 AGENTS.md | 1 MCPs | 21 Skills
  Line 4: ✓ Read ×8 | ✓ Shell ×4 | ✓ Edit ×2 | ✓ Write ×1 | +1 more
  Line 5: ◐ Subagent [model] (12s)   (only when active or completed within 60s)

Reads AGY's native dual quota pools (Gemini + 3P) directly from stdin.quota,
with optional fallback to local LanguageServer quota cache (`--refresh-quota`).
"""
from __future__ import annotations

import http.client
import json
import os
import queue
import re
import ssl
import subprocess
import sys
import threading
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stdin, "reconfigure"):
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")

# ── Paths & Constants ──────────────────────────────────────────────────────────

SCRIPT_DIR = Path(__file__).resolve().parent
HOME_DIR = Path.home()

DEFAULT_HUD_DIR = Path(os.environ.get("AGY_HUD_DIR", str(HOME_DIR / ".antigravity" / "agy-hud")))
QUOTA_CACHE_FILE = Path(
    os.environ.get("AGY_QUOTA_CACHE", str(HOME_DIR / ".antigravity" / "quota-cache.json"))
)
STATUS_STATE_FILE = Path(
    os.environ.get("AGY_STATUS_STATE", str(HOME_DIR / ".antigravity" / "status-state.json"))
)
SESSION_STARTS_FILE = Path(
    os.environ.get("AGY_HUD_SESSIONS", str(DEFAULT_HUD_DIR / "session-starts.json"))
)
SUBAGENT_STATE_FILE = Path(
    os.environ.get("AGY_HUD_SUBAGENT_STATE", str(DEFAULT_HUD_DIR / "subagent-state.json"))
)
QUOTA_LOCK_FILE = Path(
    os.environ.get("AGY_QUOTA_LOCK", str(DEFAULT_HUD_DIR / "quota-refresh.lock"))
)

QUOTA_MAX_AGE_SECONDS = float(os.environ.get("AGY_QUOTA_MAX_AGE_SECONDS", "900"))
QUOTA_REFRESH_INTERVAL_SECONDS = float(os.environ.get("AGY_QUOTA_REFRESH_INTERVAL_SECONDS", "45"))
USER_STATUS_PATH = "/exa.language_server_pb.LanguageServerService/GetUserStatus"

# ── ANSI Colors (matching claude-hud palette) ──────────────────────────────────

RESET = "\x1b[0m"
DIM = "\x1b[2m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"
BRIGHT_BLUE = "\x1b[94m"
BRIGHT_MAGENTA = "\x1b[95m"
ORANGE = "\x1b[38;5;208m"

ANSI_BY_NAME = {
    "dim": DIM,
    "red": RED,
    "green": GREEN,
    "yellow": YELLOW,
    "magenta": MAGENTA,
    "cyan": CYAN,
    "brightBlue": BRIGHT_BLUE,
    "brightMagenta": BRIGHT_MAGENTA,
    "orange": ORANGE,
}

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")

EFFORT_SYMBOLS = {
    "low": "○",
    "medium": "◔",
    "high": "◑",
    "xhigh": "◕",
    "max": "●",
}

FRIENDLY_TOOL_NAMES = {
    "run_command": "Shell",
    "read_file": "Read",
    "write_file": "Write",
    "edit_file": "Edit",
    "search_web": "WebSearch",
    "invoke_subagent": "Agent",
    "define_subagent": "DefineAgent",
    "manage_subagents": "Agents",
    "manage_task": "Task",
    "ask_question": "Ask",
    "run_workflow": "Workflow",
    "schedule": "Schedule",
    "send_message": "Message",
    "swarm": "Swarm",
}

I18N: dict[str, dict[str, str]] = {
    "zh-Hans": {
        "label.context": "上下文",
        "label.usage": "用量",
        "label.rules": "规则",
        "label.hooks": "钩子",
        "label.skills": "Skills",
        "format.in": "输入",
        "format.out": "输出",
        "format.cache": "缓存",
        "status.allTodosComplete": "所有任务已完成",
        "init.initializing": "[agy-hud] 正在初始化...",
    },
    "en": {
        "label.context": "Context",
        "label.usage": "Usage",
        "label.rules": "rules",
        "label.hooks": "hooks",
        "label.skills": "Skills",
        "format.in": "in",
        "format.out": "out",
        "format.cache": "cache",
        "status.allTodosComplete": "All todos complete",
        "init.initializing": "[agy-hud] Initializing...",
    },
}

DEFAULT_CONFIG: dict[str, Any] = {
    "language": "zh-Hans",
    "lineLayout": "expanded",
    "showSeparators": False,
    "pathLevels": 1,
    "maxWidth": None,
    "gitStatus": {
        "enabled": True,
        "showDirty": True,
        "showAheadBehind": False,
        "showFileStats": True,
    },
    "display": {
        "showModel": True,
        "showEffortLevel": True,
        "effortFormat": "full",
        "showProject": True,
        "showSessionName": True,
        "showSandbox": True,
        "showAgentState": False,
        "showDuration": True,
        "showContextBar": True,
        "contextValue": "percent",
        "contextWarningThreshold": 70,
        "contextCriticalThreshold": 85,
        "showTokenBreakdown": True,
        "showUsage": True,
        "quotaPools": "both",
        "usageValue": "remaining",
        "showQuotaBars": False,
        "showPlanTier": True,
        "fallbackLocalServerQuota": False,
        "showConfigCounts": True,
        "showSkillCounts": True,
        "showTools": True,
        "friendlyToolNames": True,
        "toolsMaxVisible": 4,
        "toolNameMaxLength": 0,
        "showAgents": True,
        "agentCompletedRetentionSeconds": 60,
        "showTodos": True,
        "showSessionTokens": False,
    },
    "colors": {
        "model": "cyan",
        "project": "yellow",
        "git": "magenta",
        "gitBranch": "cyan",
        "context": "green",
        "usage": "brightBlue",
        "usageWarning": "brightMagenta",
        "warning": "yellow",
        "critical": "red",
        "label": "dim",
        "barFilled": "█",
        "barEmpty": "░",
    },
}


# ── Config & i18n ──────────────────────────────────────────────────────────────

def _no_window_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {}


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if isinstance(out.get(k), dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config() -> dict[str, Any]:
    candidates = []
    env_cfg = os.environ.get("AGY_HUD_CONFIG")
    if env_cfg:
        candidates.append(Path(env_cfg))
    candidates.extend([
        DEFAULT_HUD_DIR / "config.json",
        SCRIPT_DIR / "config.json",
    ])
    for cfg_path in candidates:
        try:
            if cfg_path.is_file():
                with cfg_path.open("r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    return _deep_merge(DEFAULT_CONFIG, raw)
        except Exception:
            continue
    return _deep_merge(DEFAULT_CONFIG, {})


def tr(config: dict[str, Any], key: str) -> str:
    lang = str(config.get("language") or "zh-Hans")
    table = I18N["zh-Hans"] if lang.lower().startswith("zh") else I18N["en"]
    return table.get(key, key)


# ── Color & Width Utilities ────────────────────────────────────────────────────

def resolve_ansi(value: Any, fallback: str) -> str:
    if isinstance(value, int) and 0 <= value <= 255:
        return f"\x1b[38;5;{value}m"
    if isinstance(value, str):
        if re.fullmatch(r"#[0-9a-fA-F]{6}", value):
            r = int(value[1:3], 16)
            g = int(value[3:5], 16)
            b = int(value[5:7], 16)
            return f"\x1b[38;2;{r};{g};{b}m"
        return ANSI_BY_NAME.get(value, fallback)
    return fallback


def colorize(text: str, ansi: str) -> str:
    return f"{ansi}{text}{RESET}"


def c_model(text: str, colors: dict[str, Any]) -> str:
    return colorize(text, resolve_ansi(colors.get("model"), CYAN))


def c_project(text: str, colors: dict[str, Any]) -> str:
    return colorize(text, resolve_ansi(colors.get("project"), YELLOW))


def c_git(text: str, colors: dict[str, Any]) -> str:
    return colorize(text, resolve_ansi(colors.get("git"), MAGENTA))


def c_git_branch(text: str, colors: dict[str, Any]) -> str:
    return colorize(text, resolve_ansi(colors.get("gitBranch"), CYAN))


def c_label(text: str, colors: dict[str, Any]) -> str:
    return colorize(text, resolve_ansi(colors.get("label"), DIM))


def context_ansi(used_pct: float, display: dict[str, Any], colors: dict[str, Any]) -> str:
    crit = float(display.get("contextCriticalThreshold", 85))
    warn = float(display.get("contextWarningThreshold", 70))
    if used_pct >= crit:
        return resolve_ansi(colors.get("critical"), RED)
    if used_pct >= warn:
        return resolve_ansi(colors.get("warning"), YELLOW)
    return resolve_ansi(colors.get("context"), GREEN)


def quota_used_ansi(used_pct: float, colors: dict[str, Any]) -> str:
    if used_pct >= 90:
        return resolve_ansi(colors.get("critical"), RED)
    if used_pct >= 75:
        return resolve_ansi(colors.get("usageWarning"), BRIGHT_MAGENTA)
    return resolve_ansi(colors.get("usage"), BRIGHT_BLUE)


def quota_remaining_ansi(rem_pct: float, colors: dict[str, Any]) -> str:
    return quota_used_ansi(100.0 - rem_pct, colors)


def render_bar(used_pct: float, width: int, display: dict[str, Any], colors: dict[str, Any]) -> str:
    safe_pct = max(0.0, min(100.0, float(used_pct)))
    filled = round((safe_pct / 100.0) * width)
    empty = max(0, width - filled)
    filled_ch = str(colors.get("barFilled") or "█")
    empty_ch = str(colors.get("barEmpty") or "░")
    ansi = context_ansi(safe_pct, display, colors)
    return f"{ansi}{filled_ch * filled}{DIM}{empty_ch * empty}{RESET}"


def visual_width(text: str) -> int:
    plain = ANSI_RE.sub("", text)
    w = 0
    for ch in plain:
        if unicodedata.combining(ch) or ch in ("\ufe0e", "\ufe0f", "\u200d"):
            continue
        ea = unicodedata.east_asian_width(ch)
        w += 2 if ea in ("W", "F") else 1
    return w


def get_terminal_width(config: dict[str, Any], data: dict[str, Any] | None = None) -> int:
    if isinstance(data, dict):
        tw = data.get("terminal_width")
        if isinstance(tw, int) and tw > 20:
            return tw
    cfg_w = config.get("maxWidth")
    if isinstance(cfg_w, int) and cfg_w > 20:
        return cfg_w
    env_cols = os.environ.get("COLUMNS", "").strip()
    if env_cols.isdigit() and int(env_cols) > 20:
        return int(env_cols)
    return 0


def wrap_line(line: str, max_width: int) -> list[str]:
    if max_width <= 0 or visual_width(line) <= max_width:
        return [line]
    # Split on top-level separators while preserving colors
    tokens = re.split(r"( │ | \| )", line)
    if len(tokens) <= 1:
        return [line]
    lines: list[str] = []
    cur = tokens[0]
    i = 1
    while i < len(tokens):
        sep = tokens[i]
        seg = tokens[i + 1] if i + 1 < len(tokens) else ""
        candidate = cur + sep + seg
        if visual_width(candidate) <= max_width:
            cur = candidate
        else:
            lines.append(cur)
            cur = seg
        i += 2
    if cur:
        lines.append(cur)
    return lines


# ── Formatting Helpers ─────────────────────────────────────────────────────────

def format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.0f}k"
    return str(n)


def format_duration_seconds(seconds: float) -> str:
    mins = int(max(0, seconds) // 60)
    if mins < 1:
        return "<1m"
    if mins < 60:
        return f"{mins}m"
    hours, rem_mins = divmod(mins, 60)
    return f"{hours}h {rem_mins}m" if rem_mins else f"{hours}h"


def format_elapsed_short(seconds: float) -> str:
    s = max(0.0, seconds)
    if s < 1.0:
        return "<1s"
    if s < 60.0:
        return f"{round(s)}s"
    total_s = int(s)
    mins, secs = divmod(total_s, 60)
    if mins < 60:
        return f"{mins}m {secs}s"
    hours, rem_mins = divmod(mins, 60)
    return f"{hours}h {rem_mins}m"


def parse_iso_timestamp(value: Any) -> float | None:
    if isinstance(value, (int, float)) and value > 0:
        return float(value) / 1000.0 if value > 1e12 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except Exception:
            return None
    return None


def format_reset_countdown(reset_ts: float | None, now: float | None = None) -> str:
    if reset_ts is None:
        return ""
    now_ts = time.time() if now is None else now
    diff = int(reset_ts - now_ts)
    if diff <= 0:
        return "now"
    minutes = (diff + 59) // 60
    if minutes < 60:
        return f"{minutes}m"
    hours, mins = divmod(minutes, 60)
    if hours >= 24:
        days, rem_h = divmod(hours, 24)
        return f"{days}d {rem_h}h" if rem_h else f"{days}d"
    return f"{hours}h {mins}m" if mins else f"{hours}h"


def normalize_model_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (name or "").lower())


def parse_model_and_effort(raw_name: str, model_id: str = "") -> tuple[str, str]:
    """Extract clean model name and effort level from AGY display name or ID.

    Examples:
      'Gemini 4 Neon (High)' -> ('Gemini 4 Neon', 'high')
      'gemini-4-hydrogen-medium' -> ('gemini-4-hydrogen', 'medium')
    """
    name = (raw_name or model_id or "AGY").strip()
    effort = ""

    m = re.search(r"\s*\((low|medium|high|xhigh|max)\)\s*$", name, re.IGNORECASE)
    if m:
        effort = m.group(1).lower()
        name = name[: m.start()].strip()
    else:
        for candidate in (name, model_id or ""):
            m2 = re.search(r"-(low|medium|high|xhigh|max)$", candidate.strip(), re.IGNORECASE)
            if m2:
                effort = m2.group(1).lower()
                if candidate == name:
                    name = name[: m2.start()].strip()
                break

    return name or "AGY", effort


def format_project_path(cwd: str, path_levels: Any = 1) -> str:
    if not cwd:
        return ""
    norm = cwd.replace("\\", "/").rstrip("/")
    if path_levels == "full":
        home = str(HOME_DIR).replace("\\", "/").rstrip("/")
        if norm.lower().startswith(home.lower()):
            return "~" + norm[len(home):]
        return norm
    levels = int(path_levels) if isinstance(path_levels, int) and path_levels > 0 else 1
    parts = [p for p in norm.split("/") if p]
    if not parts:
        return norm
    return "/".join(parts[-levels:])


def shorten_plan_tier(plan_tier: str) -> str:
    s = (plan_tier or "").strip()
    if not s:
        return ""
    if s.lower().startswith("google "):
        return s[7:].strip()
    return s


def _sanitize_text(value: Any, max_len: int = 0) -> str:
    """Strip ANSI/control chars, collapse whitespace, and optionally truncate."""
    if value is None:
        return ""
    s = ANSI_RE.sub("", str(value))
    s = re.sub(r"[\x00-\x1f\x7f-\x9f]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    if max_len > 0 and len(s) > max_len:
        return s[: max(1, max_len - 1)].rstrip() + "…"
    return s


def format_agent_model(model: str) -> str:
    """Compact an agent model ID into a short statusline-friendly badge."""
    cleaned = _sanitize_text(model)
    if not cleaned or cleaned.lower() == "inherit":
        return ""

    candidate = re.sub(r"\[[^\]]*\]$", "", cleaned).strip()
    m_cur = re.fullmatch(
        r"claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?(?:-\d{8})?",
        candidate,
        re.IGNORECASE,
    )
    if m_cur:
        family, major, minor = m_cur.groups()
        return f"{family.lower()}-{major}.{minor}" if minor else f"{family.lower()}-{major}"

    m_leg = re.fullmatch(
        r"claude-(\d+)(?:-(\d+))?-(opus|sonnet|haiku)(?:-\d{8})?",
        candidate,
        re.IGNORECASE,
    )
    if m_leg:
        major, minor, family = m_leg.groups()
        return f"{family.lower()}-{major}.{minor}" if minor else f"{family.lower()}-{major}"

    if candidate.lower() == "claude-":
        return ""

    # Strip trailing date and effort suffixes on Gemini / general model IDs
    shortened = re.sub(r"-\d{8}$", "", candidate)
    shortened = re.sub(r"\s*\((?:low|medium|high|xhigh|max)\)\s*$", "", shortened, flags=re.IGNORECASE)
    shortened = re.sub(r"-(?:low|medium|high|xhigh|max)$", "", shortened, flags=re.IGNORECASE)
    return shortened.strip() or cleaned


def _extract_prompt_summary(prompt: str, max_len: int = 40) -> str:
    """Return a clean one-line summary from a subagent prompt or brief."""
    if not isinstance(prompt, str) or not prompt.strip():
        return ""
    for raw_line in prompt.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Strip leading markdown headings, blockquotes, list bullets, or task boxes
        while True:
            updated = re.sub(r"^(?:[#>*+-]+|\d+[.)]|\[[ xX/]\])\s*", "", line).strip()
            if updated == line:
                break
            line = updated
        if line:
            return _sanitize_text(line, max_len)
    return ""


def _find_brain_dir(data: dict[str, Any], transcript_path: Path | None) -> Path | None:
    """Locate the AGY brain directory for the active conversation."""
    if (
        transcript_path is not None
        and len(transcript_path.parents) >= 3
        and transcript_path.parent.name == "logs"
        and transcript_path.parents[1].name == ".system_generated"
    ):
        return transcript_path.parents[2]

    conv_id = str(data.get("conversation_id") or data.get("session_id") or "").strip()
    if not conv_id:
        return None
    for app_dir in ("antigravity-cli", "antigravity", "antigravity-ide"):
        candidate = HOME_DIR / ".gemini" / app_dir / "brain" / conv_id
        if candidate.is_dir():
            return candidate
    return None


def _resolve_child_transcript_path(conv_id: str, parent_transcript: Path | None) -> Path | None:
    """Locate a subagent's transcript.jsonl by conversation ID."""
    cid = (conv_id or "").strip()
    if not cid:
        return None
    if (
        parent_transcript is not None
        and len(parent_transcript.parents) >= 4
        and parent_transcript.parent.name == "logs"
        and parent_transcript.parents[1].name == ".system_generated"
    ):
        sibling = parent_transcript.parents[3] / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if sibling.is_file():
            return sibling

    for app_dir in ("antigravity-cli", "antigravity", "antigravity-ide"):
        candidate = HOME_DIR / ".gemini" / app_dir / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
        if candidate.is_file():
            return candidate
    return None


# ── Git Status ─────────────────────────────────────────────────────────────────

def _parse_porcelain_file_stats(porcelain: str) -> dict[str, int]:
    stats = {"modified": 0, "added": 0, "deleted": 0, "untracked": 0}
    for line in porcelain.splitlines():
        if len(line) < 2:
            continue
        index_ch, work_ch = line[0], line[1]
        if line.startswith("??"):
            stats["untracked"] += 1
        elif index_ch == "A":
            stats["added"] += 1
        elif index_ch == "D" or work_ch == "D":
            stats["deleted"] += 1
        elif index_ch in ("M", "R", "C") or work_ch == "M":
            stats["modified"] += 1
    return stats


def get_git_status(
    cwd: str,
    git_cfg: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not git_cfg.get("enabled", True):
        return None

    show_file_stats = bool(git_cfg.get("showFileStats", True))

    # Fast path: use native AGY stdin.vcs when provided (unless file stats are requested)
    vcs = data.get("vcs") if isinstance(data, dict) else None
    branch_out = ""
    dirty = False
    used_vcs_fast_path = False
    if isinstance(vcs, dict) and isinstance(vcs.get("branch"), str) and vcs.get("branch").strip():
        branch_out = vcs["branch"].strip()
        dirty = bool(vcs.get("dirty", False)) if git_cfg.get("showDirty", True) else False
        used_vcs_fast_path = True

    if not used_vcs_fast_path:
        if not cwd or not os.path.isdir(cwd):
            return None
        try:
            branch_out = subprocess.check_output(
                ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=0.4,
                **_no_window_kwargs(),
            ).strip()
        except Exception:
            return None
        if not branch_out:
            return None

    file_stats: dict[str, int] | None = None
    if show_file_stats and cwd and os.path.isdir(cwd):
        try:
            porcelain = subprocess.check_output(
                ["git", "-C", cwd, "-c", "core.quotePath=false", "--no-optional-locks", "status", "--porcelain"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=0.4,
                **_no_window_kwargs(),
            )
            if porcelain.strip():
                file_stats = _parse_porcelain_file_stats(porcelain)
                if git_cfg.get("showDirty", True) and any(file_stats.values()):
                    dirty = True
        except Exception:
            file_stats = None
    elif not used_vcs_fast_path and git_cfg.get("showDirty", True):
        try:
            porcelain = subprocess.check_output(
                ["git", "-C", cwd, "status", "--porcelain", "--untracked-files=no"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=0.4,
                **_no_window_kwargs(),
            ).strip()
            dirty = bool(porcelain)
        except Exception:
            dirty = False

    ahead = 0
    behind = 0
    if git_cfg.get("showAheadBehind", False) and cwd and os.path.isdir(cwd):
        try:
            ab = subprocess.check_output(
                ["git", "-C", cwd, "rev-list", "--left-right", "--count", "HEAD...@{u}"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=0.4,
                **_no_window_kwargs(),
            ).strip()
            parts = ab.split()
            if len(parts) == 2:
                ahead, behind = int(parts[0]), int(parts[1])
        except Exception:
            pass

    return {
        "branch": branch_out,
        "dirty": dirty,
        "ahead": ahead,
        "behind": behind,
        "file_stats": file_stats,
    }


# ── AGY Environment Config Counter ─────────────────────────────────────────────

def _find_repo_root(cwd: Path) -> Path:
    cur = cwd
    for _ in range(10):
        if (cur / ".git").exists():
            return cur
        if cur.parent == cur:
            break
        cur = cur.parent
    return cwd


def _count_skills_in_dir(dir_path: Path, seen: set[str]) -> int:
    count = 0
    if not dir_path.is_dir():
        return 0
    try:
        for child in dir_path.iterdir():
            if not child.is_dir():
                continue
            skill_md = child / "SKILL.md"
            if skill_md.is_file():
                resolved = str(skill_md.resolve()).lower()
                if resolved not in seen:
                    seen.add(resolved)
                    count += 1
    except Exception:
        pass
    return count


def _count_mcp_in_file(path: Path, seen_names: set[str]) -> None:
    try:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        servers = obj.get("mcpServers") if isinstance(obj, dict) else None
        if isinstance(servers, dict):
            for name in servers.keys():
                seen_names.add(str(name))
    except Exception:
        pass


def _count_hooks_in_file(path: Path, seen_names: set[str]) -> None:
    try:
        if not path.is_file():
            return
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        if isinstance(obj, dict):
            for name, spec in obj.items():
                if isinstance(spec, dict) and spec.get("enabled", True) is not False:
                    seen_names.add(f"{path}:{name}")
    except Exception:
        pass


def _scan_plugins_dir(
    plugins_dir: Path,
    mcp_seen: set[str],
    hooks_seen: set[str],
    rules_seen: set[str],
    skills_seen: set[str],
) -> None:
    """Count enabled plugin MCP servers, hooks, skills, and rules under plugins_dir."""
    if not plugins_dir.is_dir():
        return
    try:
        for pdir in plugins_dir.iterdir():
            if not pdir.is_dir():
                continue
            pjson = pdir / "plugin.json"
            if pjson.is_file():
                try:
                    with pjson.open("r", encoding="utf-8") as f:
                        meta = json.load(f)
                    if isinstance(meta, dict) and meta.get("disabled") is True:
                        continue
                except Exception:
                    pass
            _count_mcp_in_file(pdir / "mcp_config.json", mcp_seen)
            _count_hooks_in_file(pdir / "hooks.json", hooks_seen)
            _count_skills_in_dir(pdir / "skills", skills_seen)
            prules = pdir / "rules"
            if prules.is_dir():
                for rf in prules.glob("*.md"):
                    rules_seen.add(str(rf.resolve()).lower())
    except Exception:
        pass


def count_agy_configs(cwd_str: str) -> dict[str, int]:
    agents_md_seen: set[str] = set()
    rules_seen: set[str] = set()
    skills_seen: set[str] = set()
    mcp_seen: set[str] = set()
    hooks_seen: set[str] = set()

    # Directories to walk from cwd up to repo root
    dirs_to_scan: list[Path] = []
    if cwd_str and os.path.isdir(cwd_str):
        cwd = Path(cwd_str).resolve()
        root = _find_repo_root(cwd)
        cur = cwd
        while True:
            dirs_to_scan.append(cur)
            if cur == root or cur.parent == cur:
                break
            cur = cur.parent

    # Global config dir
    global_cfg_dir = HOME_DIR / ".gemini" / "config"
    if global_cfg_dir.is_dir():
        for fname in ("AGENTS.md", "GEMINI.md"):
            p = global_cfg_dir / fname
            if p.is_file():
                agents_md_seen.add(str(p.resolve()).lower())
        _count_mcp_in_file(global_cfg_dir / "mcp_config.json", mcp_seen)
        _count_hooks_in_file(global_cfg_dir / "hooks.json", hooks_seen)
        _count_skills_in_dir(global_cfg_dir / "skills", skills_seen)
        _scan_plugins_dir(global_cfg_dir / "plugins", mcp_seen, hooks_seen, rules_seen, skills_seen)

    for d in dirs_to_scan:
        for fname in ("AGENTS.md", "GEMINI.md"):
            p = d / fname
            if p.is_file():
                agents_md_seen.add(str(p.resolve()).lower())

        for dot_name in (".agents", ".agent", "_agents", "_agent"):
            dot_dir = d / dot_name
            if not dot_dir.is_dir():
                continue

            # Rules
            rules_dir = dot_dir / "rules"
            if rules_dir.is_dir():
                for rf in rules_dir.glob("*.md"):
                    rules_seen.add(str(rf.resolve()).lower())

            # Direct skills
            _count_skills_in_dir(dot_dir / "skills", skills_seen)

            # skills.json entries
            skills_json = dot_dir / "skills.json"
            if skills_json.is_file():
                try:
                    with skills_json.open("r", encoding="utf-8") as f:
                        sj = json.load(f)
                    for entry in sj.get("entries", []) or []:
                        raw_p = entry.get("path", "") if isinstance(entry, dict) else ""
                        if not raw_p:
                            continue
                        expanded = Path(os.path.expanduser(raw_p))
                        if not expanded.is_absolute():
                            expanded = d / expanded
                        _count_skills_in_dir(expanded, skills_seen)
                except Exception:
                    pass

            # Direct MCP & Hooks
            _count_mcp_in_file(dot_dir / "mcp_config.json", mcp_seen)
            _count_hooks_in_file(dot_dir / "hooks.json", hooks_seen)

            # Plugins
            _scan_plugins_dir(dot_dir / "plugins", mcp_seen, hooks_seen, rules_seen, skills_seen)

    return {
        "agentsMdCount": len(agents_md_seen),
        "rulesCount": len(rules_seen),
        "skillsCount": len(skills_seen),
        "mcpCount": len(mcp_seen),
        "hooksCount": len(hooks_seen),
    }


# ── AGY Transcript Parser ──────────────────────────────────────────────────────

def resolve_transcript_path(data: dict[str, Any]) -> Path | None:
    tp = data.get("transcript_path") or data.get("transcriptPath")
    if isinstance(tp, str) and tp.strip():
        p = Path(os.path.expanduser(tp.strip()))
        if p.is_file():
            return p

    conv_id = data.get("conversation_id") or data.get("session_id") or ""
    if not isinstance(conv_id, str) or not conv_id.strip():
        return None
    conv_id = conv_id.strip()

    for app_dir in ("antigravity-cli", "antigravity", "antigravity-ide"):
        candidate = (
            HOME_DIR / ".gemini" / app_dir / "brain" / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
        )
        if candidate.is_file():
            return candidate
    return None


def _clean_arg_value(val: Any) -> str:
    if not isinstance(val, str):
        return ""
    s = val.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        try:
            s = json.loads(s)
        except Exception:
            s = s[1:-1]
    return str(s).strip()


def _extract_tool_target(name: str, args: dict[str, Any]) -> str:
    if not isinstance(args, dict):
        return ""
    if name in ("read_file", "write_file", "edit_file"):
        p = _clean_arg_value(args.get("Path", ""))
        if p:
            return (os.path.basename(p.replace("\\", "/")) or p).strip()
    if name == "run_command":
        summary = _clean_arg_value(args.get("toolSummary", ""))
        if summary:
            return summary[:24].strip()
        cmd = _clean_arg_value(args.get("CommandLine", ""))
        if cmd:
            return cmd.splitlines()[0][:24].strip()
    if name == "search_web":
        q = _clean_arg_value(args.get("query", ""))
        if q:
            return q[:24].strip()
    if name == "invoke_subagent":
        role = _clean_arg_value(args.get("Role", ""))
        if role:
            return role[:24].strip()
    return ""


UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
CONV_ID_RE = re.compile(
    r"(?:conversation[_-]?id|conversationId)[\"'=:\s]+([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})",
    re.IGNORECASE,
)

RUNNING_SUBAGENT_STATUSES = {
    "running", "working", "active", "in_progress", "waiting", "tool_use", "reviewing", "initializing", "pending",
}
ERROR_SUBAGENT_STATUSES = {"error", "failed"}
COMPLETED_SUBAGENT_STATUSES = {"completed", "done", "finished", "killed", "cancelled", "canceled", "stopped"}


def _read_dedup_transcript_entries(transcript_path: Path | None) -> list[dict[str, Any]]:
    if not transcript_path or not transcript_path.is_file():
        return []
    raw_entries: list[dict[str, Any]] = []
    try:
        with transcript_path.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if isinstance(obj, dict):
                        raw_entries.append(obj)
                except json.JSONDecodeError:
                    continue
    except Exception:
        return []

    if not raw_entries:
        return []

    entries: list[dict[str, Any]] = []
    idx_pos: dict[int, int] = {}
    for obj in raw_entries:
        si = obj.get("step_index")
        if isinstance(si, int):
            if si in idx_pos:
                entries[idx_pos[si]] = obj
            else:
                idx_pos[si] = len(entries)
                entries.append(obj)
        else:
            entries.append(obj)
    return entries


def _inspect_subagent_transcript(conv_id: str, parent_transcript: Path | None) -> dict[str, Any]:
    child_path = _resolve_child_transcript_path(conv_id, parent_transcript)
    entries = _read_dedup_transcript_entries(child_path)
    if not entries:
        return {}

    start_ts: float | None = None
    last_ts: float | None = None
    model = ""
    last_pr_idx: int | None = None

    for idx, entry in enumerate(entries):
        ts = parse_iso_timestamp(entry.get("created_at"))
        if ts is not None:
            if start_ts is None:
                start_ts = ts
            last_ts = ts
        if entry.get("type") == "PLANNER_RESPONSE":
            last_pr_idx = idx
            if entry.get("model"):
                model = str(entry["model"])

    active_tool = ""
    child_done = False
    end_ts: float | None = None

    if last_pr_idx is not None:
        last_pr = entries[last_pr_idx]
        tail = entries[last_pr_idx + 1 :]
        has_later_user = any(e.get("type") == "USER_INPUT" for e in tail)
        tool_calls = last_pr.get("tool_calls") or []
        if isinstance(tool_calls, list) and tool_calls:
            result_steps = [
                e for e in tail if e.get("source") == "MODEL" and e.get("type") != "PLANNER_RESPONSE"
            ]
            chosen_tc: dict[str, Any] | None = None
            for tc_idx, tc in enumerate(tool_calls):
                if not isinstance(tc, dict):
                    continue
                if tc_idx >= len(result_steps) or str(result_steps[tc_idx].get("status") or "").upper() == "RUNNING":
                    chosen_tc = tc
            if chosen_tc is None and isinstance(tool_calls[-1], dict):
                chosen_tc = tool_calls[-1]
            if chosen_tc is not None:
                tname_raw = str(chosen_tc.get("name") or "").strip()
                if tname_raw:
                    tname = _format_tool_name(tname_raw, {"friendlyToolNames": True})
                    targs = chosen_tc.get("args") if isinstance(chosen_tc.get("args"), dict) else {}
                    ttarget = _extract_tool_target(tname_raw, targs)
                    active_tool = f"{tname}: {ttarget}" if ttarget else tname
            child_done = False
        elif str(last_pr.get("status") or "").upper() == "DONE" and not has_later_user:
            child_done = True
            end_ts = last_ts or parse_iso_timestamp(last_pr.get("created_at"))

    return {
        "model": model,
        "start_ts": start_ts,
        "last_ts": last_ts,
        "active_tool": active_tool,
        "child_done": child_done,
        "end_ts": end_ts,
    }


def _extract_conv_id_from_text(text: str) -> str:
    if not text:
        return ""
    m = CONV_ID_RE.search(text)
    if m:
        return m.group(1)
    m2 = UUID_RE.search(text)
    return m2.group(0) if m2 else ""


def _parse_conv_id_list(val: Any) -> list[str]:
    if isinstance(val, list):
        return [str(x).strip() for x in val if str(x).strip()]
    if isinstance(val, str):
        s = _clean_arg_value(val)
        if s.startswith("["):
            try:
                loaded = json.loads(s)
                if isinstance(loaded, list):
                    return [str(x).strip() for x in loaded if str(x).strip()]
            except Exception:
                pass
        return UUID_RE.findall(s)
    return []


def _normalize_todo_items(raw_todos: Any) -> list[dict[str, str]]:
    if not isinstance(raw_todos, list):
        return []
    out: list[dict[str, str]] = []
    for item in raw_todos:
        if not isinstance(item, dict):
            continue
        content = _sanitize_text(item.get("content") or item.get("subject") or "")
        if not content:
            continue
        st = str(item.get("status") or "pending").lower()
        if st in ("completed", "complete", "done"):
            norm_st = "completed"
        elif st in ("in_progress", "running"):
            norm_st = "in_progress"
        else:
            norm_st = "pending"
        out.append({"content": content, "status": norm_st})
    return out


def parse_agy_transcript(transcript_path: Path | None, agent_state: str = "idle") -> dict[str, Any]:
    result: dict[str, Any] = {
        "session_start": None,
        "session_title": "",
        "tools": [],
        "agents": [],
        "todos": [],
        "last_model": "",
        "total_input_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_output_tokens": 0,
    }
    entries = _read_dedup_transcript_entries(transcript_path)
    if not entries:
        return result

    entry_idx_by_obj_id = {id(e): i for i, e in enumerate(entries)}

    # Pre-scan lifecycle events: background task completions, subagent kills, replies, titles, TodoWrite
    finished_task_steps: set[int] = set()
    killed_conv_ids: dict[str, float] = {}
    kill_all_ts: list[float] = []
    killed_swarm_names: dict[str, float] = {}
    reply_events: list[tuple[int, float, str]] = []
    fallback_todos: list[dict[str, str]] = []

    for idx, entry in enumerate(entries):
        ts = parse_iso_timestamp(entry.get("created_at"))
        if ts is not None and result["session_start"] is None:
            result["session_start"] = ts

        # Session title from transcript if emitted
        for tkey in ("conversation_title", "custom_title", "customTitle"):
            tval = entry.get(tkey)
            if isinstance(tval, str) and tval.strip():
                result["session_title"] = tval.strip()
        if entry.get("type") in ("CUSTOM_TITLE", "SESSION_TITLE", "TITLE_UPDATE") and isinstance(entry.get("title"), str):
            if entry["title"].strip():
                result["session_title"] = entry["title"].strip()

        content = str(entry.get("content") or "")
        etype = str(entry.get("type") or "").upper()
        if etype == "SYSTEM_MESSAGE" or "task-" in content:
            for m in re.finditer(
                r"task-(\d+)[\"']?\s+(?:was canceled|completed|finished|exited|failed|terminated)",
                content,
                re.IGNORECASE,
            ):
                finished_task_steps.add(int(m.group(1)))

        from_cid = _clean_arg_value(entry.get("from_conversation_id") or entry.get("subagent_id") or "")
        if from_cid and UUID_RE.fullmatch(from_cid):
            reply_events.append((idx, ts or time.time(), from_cid.lower()))
        elif etype in ("AGENT_MESSAGE", "SUBAGENT_MESSAGE") or (
            etype in ("SYSTEM_MESSAGE", "USER_INPUT", "MESSAGE")
            and etype != "PLANNER_RESPONSE"
            and re.search(r"replied|completed|finished|returned|message from|subagent|回复|完成|结果", content, re.IGNORECASE)
        ):
            for u in UUID_RE.findall(content):
                reply_events.append((idx, ts or time.time(), u.lower()))

        if etype == "PLANNER_RESPONSE":
            ev_ts = ts or time.time()
            for tc in entry.get("tool_calls") or []:
                if not isinstance(tc, dict):
                    continue
                tc_name = str(tc.get("name") or "")
                args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                if tc_name == "manage_task":
                    if _clean_arg_value(args.get("Action", "")).lower() == "kill":
                        tid = _clean_arg_value(args.get("TaskId", ""))
                        mk = re.search(r"task-(\d+)$", tid)
                        if mk:
                            finished_task_steps.add(int(mk.group(1)))
                elif tc_name == "manage_subagents":
                    act = _clean_arg_value(args.get("Action", "")).lower()
                    if act == "kill_all":
                        kill_all_ts.append(ev_ts)
                    elif act == "kill":
                        for cid in _parse_conv_id_list(args.get("ConversationIds")):
                            killed_conv_ids[cid.lower()] = ev_ts
                elif tc_name == "swarm":
                    act = _clean_arg_value(args.get("action", "")).lower()
                    if act == "kill":
                        wname = _clean_arg_value(args.get("name", ""))
                        if wname:
                            killed_swarm_names[wname] = ev_ts
                elif tc_name == "TodoWrite":
                    parsed_td = _normalize_todo_items(args.get("todos"))
                    if parsed_td:
                        fallback_todos = parsed_td

    result["todos"] = fallback_todos

    # Walk entries to aggregate planner tool calls and match subsequent result steps
    planner_indices: list[int] = []
    for idx, entry in enumerate(entries):
        if entry.get("type") == "PLANNER_RESPONSE":
            if entry.get("model"):
                result["last_model"] = str(entry["model"])
            result["total_input_tokens"] += int(entry.get("input_tokens") or 0)
            result["total_cache_read_tokens"] += int(entry.get("cache_read_tokens") or 0)
            result["total_output_tokens"] += int(entry.get("output_tokens") or 0)
            if isinstance(entry.get("tool_calls"), list) and entry["tool_calls"]:
                planner_indices.append(idx)

    for p_pos, p_idx in enumerate(planner_indices):
        entry = entries[p_idx]
        tool_calls = entry.get("tool_calls") or []
        next_p_idx = planner_indices[p_pos + 1] if p_pos + 1 < len(planner_indices) else len(entries)
        is_last_batch = p_pos == len(planner_indices) - 1

        result_steps = [
            e
            for e in entries[p_idx + 1 : next_p_idx]
            if e.get("source") == "MODEL" and e.get("type") not in ("PLANNER_RESPONSE",)
        ]
        start_ts = parse_iso_timestamp(entry.get("created_at")) or time.time()

        for tc_idx, tc in enumerate(tool_calls):
            if not isinstance(tc, dict):
                continue
            name = str(tc.get("name") or "").strip()
            if not name:
                continue
            args = tc.get("args") if isinstance(tc.get("args"), dict) else {}
            target = _extract_tool_target(name, args)

            launch_step: dict[str, Any] | None = None
            if tc_idx < len(result_steps):
                launch_step = result_steps[tc_idx]
                st = str(launch_step.get("status") or "DONE").upper()
                step_idx = launch_step.get("step_index")
                if st == "RUNNING" and step_idx not in finished_task_steps:
                    status = "running"
                elif st == "ERROR":
                    status = "error"
                else:
                    status = "completed"
                end_ts = parse_iso_timestamp(launch_step.get("created_at"))
            elif is_last_batch and agent_state == "working":
                status = "running"
                end_ts = None
            else:
                status = "completed"
                end_ts = start_ts

            result["tools"].append({
                "name": name,
                "target": target,
                "status": status,
            })

            if name == "invoke_subagent":
                role_arg = _clean_arg_value(args.get("Role", ""))
                type_arg = _clean_arg_value(args.get("TypeName", ""))
                role = role_arg or type_arg or "subagent"
                model_arg = _clean_arg_value(args.get("Model", ""))
                prompt_arg = _clean_arg_value(args.get("Prompt", ""))
                desc = _extract_prompt_summary(prompt_arg, 40)

                step_content = str(launch_step.get("content") or "") if launch_step else ""
                conv_id = _extract_conv_id_from_text(step_content)
                launch_entry_idx = entry_idx_by_obj_id.get(id(launch_step), p_idx) if launch_step else p_idx

                child_info = _inspect_subagent_transcript(conv_id, transcript_path) if conv_id else {}
                resolved_model = (
                    model_arg
                    if (model_arg and model_arg.lower() != "inherit")
                    else str(child_info.get("model") or "")
                )

                cid_lower = conv_id.lower() if conv_id else ""
                kill_ts = killed_conv_ids.get(cid_lower) if cid_lower else None
                later_kill_all = [kt for kt in kill_all_ts if kt >= start_ts]
                if later_kill_all:
                    ka = min(later_kill_all)
                    kill_ts = ka if kill_ts is None else min(kill_ts, ka)

                reply_ts: float | None = None
                if cid_lower:
                    for ev_idx, ev_ts, ev_cid in reply_events:
                        if ev_cid == cid_lower and ev_idx > launch_entry_idx:
                            reply_ts = ev_ts
                            break

                if kill_ts is not None:
                    ag_status = "completed"
                    ag_end_ts: float | None = kill_ts
                elif reply_ts is not None:
                    ag_status = "completed"
                    ag_end_ts = reply_ts
                elif child_info.get("child_done"):
                    ag_status = "completed"
                    ag_end_ts = child_info.get("end_ts") or child_info.get("last_ts") or start_ts
                elif conv_id or (is_last_batch and agent_state == "working"):
                    ag_status = "running"
                    ag_end_ts = None
                else:
                    ag_status = "completed"
                    ag_end_ts = end_ts or start_ts

                result["agents"].append({
                    "id": conv_id or f"{p_idx}:{tc_idx}",
                    "conv_id": conv_id,
                    "role": role,
                    "type_name": type_arg,
                    "model": resolved_model,
                    "description": desc,
                    "active_tool": str(child_info.get("active_tool") or ""),
                    "child_end_ts": child_info.get("end_ts") if child_info.get("child_done") else None,
                    "status": ag_status,
                    "start_ts": child_info.get("start_ts") or start_ts,
                    "end_ts": ag_end_ts,
                    "source": "transcript",
                })

            elif name == "swarm" and _clean_arg_value(args.get("action", "")).lower() == "add":
                w_base = _clean_arg_value(args.get("name", "")) or "worker"
                w_model = _clean_arg_value(args.get("model", ""))
                w_brief = _clean_arg_value(args.get("brief", ""))
                raw_tasks = args.get("tasks")
                if isinstance(raw_tasks, str):
                    try:
                        raw_tasks = json.loads(_clean_arg_value(raw_tasks))
                    except Exception:
                        raw_tasks = None
                raw_n = args.get("n")
                worker_specs: list[tuple[str, str]] = []
                if isinstance(raw_tasks, list) and len(raw_tasks) > 0:
                    for i, task_item in enumerate(raw_tasks):
                        worker_specs.append((
                            f"{w_base}-{i + 1}",
                            _extract_prompt_summary(str(task_item) or w_brief, 40),
                        ))
                else:
                    try:
                        n_val = int(raw_n) if raw_n is not None else 1
                    except (TypeError, ValueError):
                        n_val = 1
                    if n_val > 1:
                        b_sum = _extract_prompt_summary(w_brief, 40)
                        for i in range(n_val):
                            worker_specs.append((f"{w_base}-{i + 1}", b_sum))
                    else:
                        worker_specs.append((w_base, _extract_prompt_summary(w_brief, 40)))

                for w_idx, (w_name, w_desc) in enumerate(worker_specs):
                    kill_ts = killed_swarm_names.get(w_name)
                    is_killed = kill_ts is not None and kill_ts >= start_ts
                    result["agents"].append({
                        "id": f"swarm:{p_idx}:{tc_idx}:{w_idx}",
                        "conv_id": "",
                        "role": w_name,
                        "type_name": "swarm",
                        "model": w_model if (w_model and w_model.lower() != "inherit") else "",
                        "description": w_desc,
                        "active_tool": "",
                        "child_last_ts": None,
                        "status": "completed" if is_killed else "running",
                        "start_ts": start_ts,
                        "end_ts": kill_ts if is_killed else None,
                        "source": "swarm",
                    })

    return result


# ── Session Start Persistence (fallback when transcript isn't available yet) ───

def get_or_record_session_start(conv_id: str, now: float) -> float:
    if not conv_id:
        return now
    cache: dict[str, float] = {}
    try:
        if SESSION_STARTS_FILE.is_file():
            with SESSION_STARTS_FILE.open("r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                cache = {k: float(v) for k, v in loaded.items() if isinstance(v, (int, float))}
    except Exception:
        cache = {}

    if conv_id in cache:
        return cache[conv_id]

    # Prune entries older than 7 days
    cutoff = now - 7 * 86400
    cache = {k: v for k, v in cache.items() if v >= cutoff}
    cache[conv_id] = now
    try:
        SESSION_STARTS_FILE.parent.mkdir(parents=True, exist_ok=True)
        with SESSION_STARTS_FILE.open("w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
    except Exception:
        pass
    return now


# ── AGY Local Quota & External Usage (Codex) ───────────────────────────────────

def _extract_arg(command_line: str, name: str) -> str:
    match = re.search(rf"{re.escape(name)}(?:=|\s+)([^\s\"']+|\"[^\"]+\"|'[^']+')", command_line)
    return match.group(1).strip("\"'") if match else ""


def _score_candidate(pid: int, cmdline: str) -> dict[str, Any] | None:
    lower = cmdline.lower()
    is_cli = re.search(r"\bagy(?:\.exe)?(?:\s|$)", lower) is not None
    is_ls = "language_server" in lower
    if not is_cli and not is_ls:
        return None
    token = _extract_arg(cmdline, "--csrf_token")
    score = 10 + (40 if is_cli else 0) + (20 if is_ls else 0) + (10 if token else 0)
    return {"pid": pid, "csrf_token": token, "score": score, "kind": "cli" if is_cli else "language_server"}


def _find_server_candidates() -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if os.name == "nt":
        ps_cmd = (
            "Get-CimInstance Win32_Process | "
            "Where-Object { $_.CommandLine -match 'language_server|\\bagy(\\.exe)?(\\s|$)' } | "
            "ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"
        )
        try:
            out = subprocess.check_output(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                **_no_window_kwargs(),
            )
        except Exception:
            return []
        for line in out.splitlines():
            if "\t" not in line:
                continue
            pid_s, cmdline = line.split("\t", 1)
            try:
                cand = _score_candidate(int(pid_s.strip()), cmdline)
                if cand:
                    candidates.append(cand)
            except ValueError:
                continue
        return sorted(candidates, key=lambda x: x["score"], reverse=True)

    try:
        out = subprocess.check_output(["ps", "auxww"], text=True, stderr=subprocess.DEVNULL, timeout=1.5)
    except Exception:
        return []
    for line in out.splitlines():
        parts = line.split(None, 10)
        if len(parts) < 11:
            continue
        try:
            cand = _score_candidate(int(parts[1]), parts[10])
            if cand:
                candidates.append(cand)
        except ValueError:
            continue
    return sorted(candidates, key=lambda x: x["score"], reverse=True)


def _get_listening_ports(pid: int) -> list[int]:
    ports: list[int] = []
    if os.name == "nt":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "TCP"],
                text=True,
                encoding="utf-8",
                errors="replace",
                stderr=subprocess.DEVNULL,
                timeout=2.0,
                **_no_window_kwargs(),
            )
        except Exception:
            return []
        pid_str = str(pid)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING" and parts[4] == pid_str:
                m = re.search(r":(\d+)$", parts[1])
                if m:
                    port = int(m.group(1))
                    if port not in ports:
                        ports.append(port)
        return sorted(ports)

    try:
        out = subprocess.check_output(
            ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:LISTEN"],
            text=True,
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        )
    except Exception:
        return []
    for m in re.finditer(r":(\d+)\s+\(LISTEN\)", out):
        port = int(m.group(1))
        if port not in ports:
            ports.append(port)
    return sorted(ports)


def _request_user_status(port: int, csrf_token: str, use_https: bool) -> dict[str, Any]:
    body = json.dumps({"metadata": {"ideName": "antigravity", "extensionName": "antigravity", "locale": "en"}})
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Connect-Protocol-Version": "1",
    }
    if csrf_token:
        headers["X-Codeium-Csrf-Token"] = csrf_token
    if use_https:
        conn = http.client.HTTPSConnection("127.0.0.1", port, timeout=1.5, context=ssl._create_unverified_context())
    else:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=1.5)
    try:
        conn.request("POST", USER_STATUS_PATH, body, headers)
        res = conn.getresponse()
        raw = res.read().decode("utf-8", "replace")
        if not (200 <= res.status < 300):
            raise RuntimeError(f"HTTP {res.status}")
        return json.loads(raw)
    finally:
        conn.close()


def _parse_user_status_quota(response: dict[str, Any]) -> dict[str, Any]:
    user_status = response.get("userStatus", {}) or {}
    plan_info = (user_status.get("planStatus") or {}).get("planInfo") or {}
    cascade = user_status.get("cascadeModelConfigData") or {}
    models: dict[str, Any] = {}

    for model in cascade.get("clientModelConfigs") or []:
        qi = model.get("quotaInfo") or {}
        if "remainingFraction" not in qi:
            continue
        label = model.get("label") or (model.get("modelOrAlias") or {}).get("model") or "Unknown"
        remaining = max(0.0, min(100.0, float(qi.get("remainingFraction", 0)) * 100.0))
        entry: dict[str, Any] = {
            "name": label,
            "remaining_percentage": remaining,
            "source": "local_language_server",
        }
        reset_time = qi.get("resetTime")
        if reset_time:
            entry["reset_time"] = reset_time
            ts = parse_iso_timestamp(reset_time)
            if ts is not None:
                entry["refreshes_in"] = format_reset_countdown(ts)
        models[normalize_model_key(label)] = entry

    return {
        "timestamp": time.time(),
        "source": "local_language_server",
        "scope": {
            "email": user_status.get("email") or "",
            "plan_tier": plan_info.get("planName") or "",
        },
        "models": models,
    }


def refresh_quota_cache_sync(expected_email: str = "") -> dict[str, Any]:
    fallback: dict[str, Any] = {}
    expected = (expected_email or "").lower()
    for proc in _find_server_candidates():
        for port in _get_listening_ports(proc["pid"]):
            for use_https in (True, False):
                try:
                    resp = _request_user_status(port, proc.get("csrf_token", ""), use_https)
                    cache = _parse_user_status_quota(resp)
                    if not cache.get("models"):
                        continue
                    cache["source_process"] = proc.get("kind", "")
                    cache["source_port"] = port
                    email = str((cache.get("scope") or {}).get("email", "")).lower()
                    if expected and email == expected:
                        _write_json_atomic(QUOTA_CACHE_FILE, cache)
                        return cache
                    if not fallback:
                        fallback = cache
                except Exception:
                    continue
    if fallback:
        _write_json_atomic(QUOTA_CACHE_FILE, fallback)
    return fallback


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, sort_keys=True)
            f.write("\n")
        os.replace(tmp, path)
    except Exception:
        pass


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file():
            return {}
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _spawn_background_quota_refresh(expected_email: str) -> None:
    """Trigger non-blocking quota cache refresh with a 15s debounce lock."""
    now = time.time()
    try:
        if QUOTA_LOCK_FILE.is_file():
            mtime = QUOTA_LOCK_FILE.stat().st_mtime
            if now - mtime < 15.0:
                return
        QUOTA_LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        QUOTA_LOCK_FILE.write_text(str(now), encoding="utf-8")
    except Exception:
        return

    cmd = [sys.executable, str(Path(__file__).resolve()), "--refresh-quota"]
    if expected_email:
        cmd.extend(["--email", expected_email])
    kwargs: dict[str, Any] = {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "nt":
        kwargs.update(_no_window_kwargs())
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def load_agy_quota(model_name: str, data: dict[str, Any], allow_bg_refresh: bool = True) -> dict[str, Any]:
    cache = _read_json_file(QUOTA_CACHE_FILE)
    now = time.time()
    ts = float(cache.get("timestamp", 0) or 0)

    expected_email = str(data.get("email") or "")
    cache_email = str((cache.get("scope") or {}).get("email", ""))
    email_mismatch = bool(expected_email and cache_email and expected_email.lower() != cache_email.lower())

    needs_refresh = (not cache) or (not ts) or (now - ts >= QUOTA_REFRESH_INTERVAL_SECONDS) or email_mismatch
    if needs_refresh and allow_bg_refresh:
        _spawn_background_quota_refresh(expected_email)

    if not cache or email_mismatch:
        return {}
    if ts and now - ts > QUOTA_MAX_AGE_SECONDS:
        return {}

    models = cache.get("models")
    if not isinstance(models, dict):
        return {}

    wanted = normalize_model_key(model_name)
    entry = models.get(wanted)
    if not entry:
        # Also try without effort suffix
        base_model, _ = parse_model_and_effort(model_name)
        wanted_base = normalize_model_key(base_model)
        for key, val in models.items():
            kn = normalize_model_key(key)
            if kn and (kn in wanted or wanted in kn or (wanted_base and (kn in wanted_base or wanted_base in kn))):
                entry = val
                break

    if not isinstance(entry, dict) or "remaining_percentage" not in entry:
        return {}

    out = dict(entry)
    if out.get("reset_time"):
        rts = parse_iso_timestamp(out["reset_time"])
        if rts is not None:
            out["refreshes_in"] = format_reset_countdown(rts, now)
    return out


THIRD_PARTY_MODEL_RE = re.compile(r"(?:claude|gpt|anthropic|openai|\bo1\b|\bo3\b|3p)", re.IGNORECASE)


def is_third_party_model(model_id: str = "", model_name: str = "") -> bool:
    return bool(THIRD_PARTY_MODEL_RE.search(f"{model_id} {model_name}"))


def _parse_quota_bucket(bucket: Any, now: float) -> dict[str, Any] | None:
    if not isinstance(bucket, dict):
        return None
    rem_pct: float | None = None
    if bucket.get("remaining_fraction") is not None:
        try:
            rf = float(bucket["remaining_fraction"])
            if rf >= 0.0:
                rem_pct = max(0.0, min(100.0, rf * 100.0))
        except (TypeError, ValueError):
            rem_pct = None
    elif bucket.get("remaining_percentage") is not None:
        try:
            rp = float(bucket["remaining_percentage"])
            if rp >= 0.0:
                rem_pct = max(0.0, min(100.0, rp))
        except (TypeError, ValueError):
            rem_pct = None

    if rem_pct is None:
        return None

    reset_str = ""
    reset_secs = bucket.get("reset_in_seconds")
    if isinstance(reset_secs, (int, float)) and reset_secs > 0:
        reset_str = format_reset_countdown(now + float(reset_secs), now)
    else:
        for key in ("reset_time", "resets_at"):
            ts = parse_iso_timestamp(bucket.get(key))
            if ts is not None:
                reset_str = format_reset_countdown(ts, now)
                break

    return {
        "remaining_percentage": rem_pct,
        "refreshes_in": reset_str,
    }


def parse_stdin_quota_pools(data: dict[str, Any], now: float) -> dict[str, dict[str, dict[str, Any]]]:
    """Parse AGY's native dual quota pools (gemini and 3p) from stdin.quota."""
    quota = data.get("quota")
    if not isinstance(quota, dict):
        return {}

    pools: dict[str, dict[str, dict[str, Any]]] = {}
    mapping = {
        "gemini": (("5h", "gemini-5h"), ("7d", "gemini-weekly")),
        "3p": (("5h", "3p-5h"), ("7d", "3p-weekly")),
    }
    for pool_key, buckets in mapping.items():
        pool_data: dict[str, dict[str, Any]] = {}
        for short_key, raw_key in buckets:
            parsed = _parse_quota_bucket(quota.get(raw_key), now)
            if parsed is not None:
                pool_data[short_key] = parsed
        if pool_data:
            pools[pool_key] = pool_data
    return pools


def render_quota_mini_bar(pct: float, is_remaining: bool, colors: dict[str, Any], width: int = 6) -> str:
    safe_pct = max(0.0, min(100.0, float(pct)))
    filled = round((safe_pct / 100.0) * width)
    empty = max(0, width - filled)
    filled_ch = str(colors.get("barFilled") or "█")
    empty_ch = str(colors.get("barEmpty") or "░")
    ansi = quota_remaining_ansi(safe_pct, colors) if is_remaining else quota_used_ansi(safe_pct, colors)
    return f"{ansi}{filled_ch * filled}{DIM}{empty_ch * empty}{RESET}"


def write_status_state(data: dict[str, Any]) -> None:
    model_info = data.get("model", {})
    if isinstance(model_info, dict):
        model_str = str(model_info.get("display_name") or model_info.get("id") or "")
    else:
        model_str = str(model_info or "")
    state = {
        "email": data.get("email") or "",
        "plan_tier": data.get("plan_tier") or "",
        "session_id": data.get("conversation_id") or data.get("session_id") or "",
        "model": model_str,
        "timestamp": time.time(),
    }
    _write_json_atomic(STATUS_STATE_FILE, state)


# ── HUD Rendering ──────────────────────────────────────────────────────────────

def render_project_line(
    data: dict[str, Any],
    transcript: dict[str, Any],
    git_status: dict[str, Any] | None,
    config: dict[str, Any],
    now: float,
) -> str:
    display = config["display"]
    colors = config["colors"]
    parts: list[str] = []

    # 1. Model + Effort badge
    if display.get("showModel", True):
        model_info = data.get("model", {})
        if isinstance(model_info, dict):
            raw_name = str(model_info.get("display_name") or model_info.get("id") or "")
            model_id = str(model_info.get("id") or "")
        else:
            raw_name = str(model_info or "")
            model_id = ""
        if not raw_name and transcript.get("last_model"):
            raw_name = str(transcript["last_model"])
        clean_model, effort = parse_model_and_effort(raw_name, model_id)
        badge_inner = clean_model
        if display.get("showEffortLevel", True) and effort:
            sym = EFFORT_SYMBOLS.get(effort, "")
            ef_fmt = display.get("effortFormat", "full")
            if ef_fmt == "symbol" and sym:
                badge_inner = f"{clean_model} {sym}"
            elif ef_fmt == "text" or not sym:
                badge_inner = f"{clean_model} {effort}"
            else:
                badge_inner = f"{clean_model} {sym} {effort}"
        parts.append(c_model(f"[{badge_inner}]", colors))

    # 2. Project directory + Git branch
    project_seg = ""
    if display.get("showProject", True) and data.get("cwd"):
        project_seg = c_project(format_project_path(str(data["cwd"]), config.get("pathLevels", 1)), colors)
    git_seg = ""
    if git_status:
        branch_txt = git_status["branch"] + ("*" if git_status.get("dirty") else "")
        inner = [c_git_branch(branch_txt, colors)]
        if git_status.get("ahead", 0) > 0:
            inner.append(c_git_branch(f"↑{git_status['ahead']}", colors))
        if git_status.get("behind", 0) > 0:
            inner.append(c_git_branch(f"↓{git_status['behind']}", colors))
        fs_stats = git_status.get("file_stats")
        if isinstance(fs_stats, dict):
            if fs_stats.get("modified", 0) > 0:
                inner.append(colorize(f"!{fs_stats['modified']}", resolve_ansi(colors.get("warning"), YELLOW)))
            if fs_stats.get("added", 0) > 0:
                inner.append(colorize(f"+{fs_stats['added']}", resolve_ansi(colors.get("context"), GREEN)))
            if fs_stats.get("deleted", 0) > 0:
                inner.append(colorize(f"✘{fs_stats['deleted']}", resolve_ansi(colors.get("critical"), RED)))
            if fs_stats.get("untracked", 0) > 0:
                inner.append(c_label(f"?{fs_stats['untracked']}", colors))
        git_seg = f"{c_git('git:(', colors)}{' '.join(inner)}{c_git(')', colors)}"

    if project_seg and git_seg:
        parts.append(f"{project_seg} {git_seg}")
    elif project_seg:
        parts.append(project_seg)
    elif git_seg:
        parts.append(git_seg)

    # 2.5 Optional conversation / session title
    if display.get("showSessionName", True):
        raw_title = (
            data.get("conversation_title")
            or data.get("session_name")
            or data.get("title")
            or transcript.get("session_title")
            or ""
        )
        title_clean = _sanitize_text(raw_title, 32)
        if title_clean:
            parts.append(c_label(title_clean, colors))

    # 3. Optional sandbox badge
    sandbox_info = data.get("sandbox") or {}
    if display.get("showSandbox", True) and sandbox_info.get("enabled"):
        sb_text = "[sandbox:net]" if sandbox_info.get("allow_network") else "[sandbox]"
        parts.append(colorize(sb_text, resolve_ansi(colors.get("custom"), ORANGE)))

    # 4. Optional non-idle agent state
    if display.get("showAgentState", False):
        st = str(data.get("agent_state") or "idle").lower()
        if st == "working":
            parts.append(colorize("◐ working", resolve_ansi(colors.get("usage"), CYAN)))
        elif st == "waiting":
            parts.append(colorize("⏳ waiting", resolve_ansi(colors.get("warning"), YELLOW)))
        elif st == "error":
            parts.append(colorize("⚠ error", resolve_ansi(colors.get("critical"), RED)))

    # 5. Session duration
    if display.get("showDuration", True):
        start_ts = transcript.get("session_start")
        if start_ts is None:
            conv_id = str(data.get("conversation_id") or data.get("session_id") or "")
            start_ts = get_or_record_session_start(conv_id, now)
        dur = format_duration_seconds(now - start_ts)
        parts.append(c_label(f"⏱ {dur}", colors))

    return " │ ".join(parts)


def render_context_segment(data: dict[str, Any], transcript: dict[str, Any], config: dict[str, Any]) -> str:
    display = config["display"]
    colors = config["colors"]
    cw = data.get("context_window") or {}

    if cw.get("used_percentage") is not None:
        used_pct = max(0.0, min(100.0, float(cw["used_percentage"])))
    elif cw.get("remaining_percentage") is not None:
        used_pct = max(0.0, min(100.0, 100.0 - float(cw["remaining_percentage"])))
    else:
        ctx_limit = float(cw.get("context_window_size") or 0)
        raw_total = float(cw.get("total_tokens") or cw.get("total_input_tokens") or 0)
        used_pct = max(0.0, min(100.0, (raw_total / ctx_limit) * 100.0)) if (ctx_limit > 0 and raw_total > 0) else 0.0

    val_ansi = context_ansi(used_pct, display, colors)
    ctx_val_mode = str(display.get("contextValue", "percent")).lower()
    ctx_limit = int(cw.get("context_window_size") or 0)
    cur = cw.get("current_usage") if isinstance(cw.get("current_usage"), dict) else {}
    cur_tokens = (
        int(cur.get("input_tokens") or 0)
        + int(cur.get("cache_creation_input_tokens") or 0)
        + int(cur.get("cache_read_input_tokens") or 0)
    )
    used_tokens = int(
        cur_tokens
        or cw.get("used_tokens")
        or cw.get("total_tokens")
        or 0
    )
    if used_tokens <= 0 and ctx_limit > 0 and used_pct > 0:
        used_tokens = round(ctx_limit * (used_pct / 100.0))

    if ctx_val_mode == "remaining":
        val_text = f"{100 - round(used_pct)}%"
    elif ctx_val_mode == "tokens" and ctx_limit > 0:
        val_text = f"{format_tokens(used_tokens)}/{format_tokens(ctx_limit)}"
    elif ctx_val_mode == "both" and ctx_limit > 0:
        val_text = f"{round(used_pct)}% ({format_tokens(used_tokens)}/{format_tokens(ctx_limit)})"
    else:
        val_text = f"{round(used_pct)}%"
    val_display = f"{val_ansi}{val_text}{RESET}"

    lbl = c_label(tr(config, "label.context"), colors)
    if display.get("showContextBar", True):
        bar = render_bar(used_pct, 8, display, colors)
        seg = f"{lbl} {bar} {val_display}"
    else:
        seg = f"{lbl} {val_display}"

    crit = float(display.get("contextCriticalThreshold", 85))
    if display.get("showTokenBreakdown", True) and used_pct >= crit:
        cur = cw.get("current_usage") if isinstance(cw.get("current_usage"), dict) else {}
        in_t = int(cur.get("input_tokens") or cw.get("total_input_tokens") or transcript.get("total_input_tokens") or 0)
        out_t = int(cur.get("output_tokens") or cw.get("total_output_tokens") or transcript.get("total_output_tokens") or 0)
        if in_t or out_t:
            seg += c_label(
                f" ({tr(config, 'format.in')}: {format_tokens(in_t)}, {tr(config, 'format.out')}: {format_tokens(out_t)})",
                colors,
            )
    return seg


def _format_pool_string(
    pool_label: str,
    pool_data: dict[str, dict[str, Any]],
    *,
    is_active: bool,
    mode: str,
    show_bars: bool,
    colors: dict[str, Any],
) -> str:
    is_remaining = mode != "used"
    lbl = colorize(pool_label, resolve_ansi(colors.get("model"), CYAN)) if is_active else c_label(pool_label, colors)
    buckets: list[str] = []
    for bucket_name in ("5h", "7d"):
        info = pool_data.get(bucket_name)
        if not info:
            continue
        rem = float(info["remaining_percentage"])
        if is_remaining:
            pct_val = max(0, min(100, round(rem)))
            ansi = quota_remaining_ansi(rem, colors)
        else:
            pct_val = max(0, min(100, round(100.0 - rem)))
            ansi = quota_used_ansi(pct_val, colors)
        bar_str = f"{render_quota_mini_bar(pct_val, is_remaining, colors)} " if show_bars else ""
        rst = str(info.get("refreshes_in") or "")
        s = f"{c_label(f'{bucket_name}:', colors)} {bar_str}{ansi}{pct_val}%{RESET}"
        if rst:
            s += f" {c_label(f'({rst})', colors)}"
        buckets.append(s)
    return f"{lbl} {' | '.join(buckets)}" if buckets else ""


def render_usage_segment(
    data: dict[str, Any],
    stdin_pools: dict[str, dict[str, dict[str, Any]]],
    agy_quota_fallback: dict[str, Any],
    config: dict[str, Any],
) -> str:
    display = config["display"]
    colors = config["colors"]
    if not display.get("showUsage", True):
        return ""

    mode = str(display.get("usageValue", "remaining"))
    show_bars = bool(display.get("showQuotaBars", False))
    pool_mode = str(display.get("quotaPools", "both")).lower()

    model_info = data.get("model", {})
    if isinstance(model_info, dict):
        raw_name = str(model_info.get("display_name") or "")
        model_id = str(model_info.get("id") or "")
    else:
        raw_name = str(model_info or "")
        model_id = ""

    active_key = "3p" if is_third_party_model(model_id, raw_name) else "gemini"
    secondary_key = "gemini" if active_key == "3p" else "3p"

    segments: list[str] = []

    if stdin_pools:
        if pool_mode == "active":
            chosen = [active_key] if stdin_pools.get(active_key) else ([secondary_key] if stdin_pools.get(secondary_key) else [])
        else:
            chosen = [k for k in (active_key, secondary_key) if stdin_pools.get(k)]

        for k in chosen:
            label_text = "Gemini" if k == "gemini" else "3P"
            formatted = _format_pool_string(
                label_text,
                stdin_pools[k],
                is_active=(k == active_key),
                mode=mode,
                show_bars=show_bars,
                colors=colors,
            )
            if formatted:
                segments.append(formatted)
    elif "remaining_percentage" in agy_quota_fallback:
        rem = float(agy_quota_fallback["remaining_percentage"])
        if mode == "used":
            pct_val = max(0, min(100, round(100.0 - rem)))
            ansi = quota_used_ansi(pct_val, colors)
            tag = "AGY:"
        else:
            pct_val = max(0, min(100, round(rem)))
            ansi = quota_remaining_ansi(rem, colors)
            tag = "AGY余:"
        reset_in = str(agy_quota_fallback.get("refreshes_in") or "")
        q_txt = f"{c_label(tag, colors)} {ansi}{pct_val}%{RESET}"
        if reset_in:
            q_txt += f" {c_label(f'({reset_in})', colors)}"
        segments.append(q_txt)

    if not segments and display.get("showPlanTier", True) and data.get("plan_tier"):
        plan_short = shorten_plan_tier(str(data["plan_tier"]))
        if plan_short:
            segments.append(c_label(plan_short, colors))

    if display.get("showSessionTokens", False):
        cw = data.get("context_window") or {}
        in_t = int(cw.get("total_input_tokens") or 0)
        out_t = int(cw.get("total_output_tokens") or 0)
        if in_t or out_t:
            segments.append(c_label(f"↑{format_tokens(in_t)} ↓{format_tokens(out_t)}", colors))

    return " │ ".join(segments)


def render_environment_line(counts: dict[str, int], config: dict[str, Any]) -> str:
    display = config["display"]
    colors = config["colors"]
    if not display.get("showConfigCounts", True):
        return ""
    parts: list[str] = []
    if counts.get("agentsMdCount", 0) > 0:
        parts.append(c_label(f"{counts['agentsMdCount']} AGENTS.md", colors))
    if counts.get("rulesCount", 0) > 0:
        parts.append(c_label(f"{counts['rulesCount']} {tr(config, 'label.rules')}", colors))
    if counts.get("mcpCount", 0) > 0:
        parts.append(c_label(f"{counts['mcpCount']} MCPs", colors))
    if display.get("showSkillCounts", True) and counts.get("skillsCount", 0) > 0:
        parts.append(c_label(f"{counts['skillsCount']} {tr(config, 'label.skills')}", colors))
    if counts.get("hooksCount", 0) > 0:
        parts.append(c_label(f"{counts['hooksCount']} {tr(config, 'label.hooks')}", colors))
    return " | ".join(parts)


def _format_tool_name(name: str, display: dict[str, Any]) -> str:
    if re.match(r"^mcp__.+__.+$", name):
        out = name.split("__")[-1]
    elif display.get("friendlyToolNames", True):
        out = FRIENDLY_TOOL_NAMES.get(name, name)
    else:
        out = name
    max_len = int(display.get("toolNameMaxLength") or 0)
    if max_len > 0 and len(out) > max_len:
        return out[: max(1, max_len - 1)] + "…"
    return out


def render_tools_line(transcript: dict[str, Any], config: dict[str, Any]) -> str:
    display = config["display"]
    colors = config["colors"]
    if not display.get("showTools", True):
        return ""
    tools = transcript.get("tools") or []
    if not tools:
        return ""

    parts: list[str] = []
    running = [t for t in tools if t.get("status") == "running"]
    completed = [t for t in tools if t.get("status") in ("completed", "error")]

    warn_ansi = resolve_ansi(colors.get("warning"), YELLOW)
    usage_ansi = resolve_ansi(colors.get("usage"), CYAN)
    ctx_ansi = resolve_ansi(colors.get("context"), GREEN)

    for tool in running[-2:]:
        tname = _format_tool_name(str(tool["name"]), display)
        target = str(tool.get("target") or "")
        target_str = c_label(f": {target}", colors) if target else ""
        parts.append(f"{colorize('◐', warn_ansi)} {colorize(tname, usage_ansi)}{target_str}")

    counts: dict[str, int] = {}
    for tool in completed:
        tname = _format_tool_name(str(tool["name"]), display)
        counts[tname] = counts.get(tname, 0) + 1

    sorted_tools = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    max_vis = int(display.get("toolsMaxVisible", 4))
    visible = sorted_tools if max_vis == 0 else sorted_tools[:max_vis]
    for tname, cnt in visible:
        parts.append(f"{colorize('✓', ctx_ansi)} {tname} {c_label(f'×{cnt}', colors)}")

    hidden = 0 if max_vis == 0 else max(0, len(sorted_tools) - len(visible))
    if hidden > 0:
        parts.append(c_label(f"+{hidden} more", colors))

    return " | ".join(parts)


def _load_subagent_state() -> dict[str, Any]:
    data = _read_json_file(SUBAGENT_STATE_FILE)
    return data if isinstance(data, dict) else {}


def _save_subagent_state(state: dict[str, Any], now: float) -> None:
    cutoff = now - 86400
    pruned = {
        k: v
        for k, v in state.items()
        if isinstance(v, dict) and float(v.get("updated_at") or 0) >= cutoff
    }
    _write_json_atomic(SUBAGENT_STATE_FILE, pruned)


def _normalize_subagent_status(raw_status: Any) -> str:
    st = str(raw_status or "running").strip().lower()
    if st in ERROR_SUBAGENT_STATUSES:
        return "error"
    if st in COMPLETED_SUBAGENT_STATUSES:
        return "completed"
    if st in RUNNING_SUBAGENT_STATUSES:
        return "running"
    return "running"


def merge_subagents(
    data: dict[str, Any],
    transcript: dict[str, Any],
    now: float,
) -> list[dict[str, Any]]:
    """Merge native stdin.subagents (authoritative live state) with transcript subagent metadata."""
    agents: list[dict[str, Any]] = [dict(a) for a in (transcript.get("agents") or []) if isinstance(a, dict)]

    has_stdin_subagents = False
    raw_stdin_agents: list[Any] = []
    if isinstance(data.get("subagents"), list):
        has_stdin_subagents = True
        raw_stdin_agents = data["subagents"]
    elif isinstance(data.get("subagent_info"), dict) and isinstance(data["subagent_info"].get("subagents"), list):
        has_stdin_subagents = True
        raw_stdin_agents = data["subagent_info"]["subagents"]

    if not has_stdin_subagents:
        return agents

    session_key = str(data.get("conversation_id") or data.get("session_id") or "default")
    all_state = _load_subagent_state()
    sess_state = dict(all_state.get(session_key) or {})
    cached_agents = dict(sess_state.get("agents") or {})
    state_dirty = False

    def _cache_key(item_cid: str, item_role: str, fallback_id: str) -> str:
        if item_cid:
            return f"cid:{item_cid.lower()}"
        if item_role:
            return f"role:{item_role.lower()}"
        return f"id:{fallback_id}"

    matched_indices: set[int] = set()

    for idx, raw in enumerate(raw_stdin_agents):
        if not isinstance(raw, dict):
            continue
        cid = str(raw.get("conversation_id") or raw.get("conversationId") or raw.get("id") or "").strip()
        raw_role = str(raw.get("role") or raw.get("name") or raw.get("type_name") or raw.get("type") or "").strip()
        raw_name = str(raw.get("name") or "").strip()
        role = raw_role or raw_name or "subagent"
        norm_status = _normalize_subagent_status(raw.get("status"))
        stdin_model = str(raw.get("model") or "").strip()
        stdin_desc = _sanitize_text(raw.get("description") or raw.get("task") or raw.get("summary") or "", 40)
        stdin_start = parse_iso_timestamp(raw.get("started_at") or raw.get("start_time") or raw.get("created_at"))
        stdin_end = parse_iso_timestamp(raw.get("ended_at") or raw.get("end_time"))

        match_idx: int | None = None
        if cid:
            cid_low = cid.lower()
            for ai in range(len(agents) - 1, -1, -1):
                if ai in matched_indices:
                    continue
                if str(agents[ai].get("conv_id") or "").lower() == cid_low:
                    match_idx = ai
                    break
        if match_idx is None:
            candidates = {x.lower() for x in (raw_role, raw_name) if x}
            if candidates:
                for ai in range(len(agents) - 1, -1, -1):
                    if ai in matched_indices:
                        continue
                    ag_role = str(agents[ai].get("role") or "").lower()
                    ag_type = str(agents[ai].get("type_name") or "").lower()
                    if ag_role in candidates or ag_type in candidates:
                        match_idx = ai
                        break

        ckey = _cache_key(cid, role, f"stdin:{idx}")
        c_entry = dict(cached_agents.get(ckey) or {})

        if match_idx is not None:
            matched_indices.add(match_idx)
            ag = agents[match_idx]
            if cid and not ag.get("conv_id"):
                ag["conv_id"] = cid
            if stdin_model and (not ag.get("model") or stdin_model.lower() != "inherit"):
                ag["model"] = stdin_model
            if stdin_desc and not ag.get("description"):
                ag["description"] = stdin_desc
            if stdin_start is not None:
                ag["start_ts"] = stdin_start
            elif c_entry.get("start_ts") is not None and not ag.get("start_ts"):
                ag["start_ts"] = float(c_entry["start_ts"])

            ag["status"] = norm_status
            if norm_status == "running":
                ag["end_ts"] = None
                if c_entry.get("end_ts") is not None:
                    c_entry.pop("end_ts", None)
                    state_dirty = True
            else:
                resolved_end = stdin_end or ag.get("end_ts") or c_entry.get("end_ts") or ag.get("child_end_ts") or now
                ag["end_ts"] = float(resolved_end)
                if c_entry.get("end_ts") != ag["end_ts"]:
                    c_entry["end_ts"] = ag["end_ts"]
                    state_dirty = True

            if c_entry.get("start_ts") != ag.get("start_ts"):
                c_entry["start_ts"] = float(ag.get("start_ts") or now)
                state_dirty = True
            cached_agents[ckey] = c_entry
        else:
            start_ts = stdin_start or (float(c_entry["start_ts"]) if c_entry.get("start_ts") is not None else now)
            if norm_status == "running":
                end_ts = None
                if c_entry.get("end_ts") is not None:
                    c_entry.pop("end_ts", None)
                    state_dirty = True
            else:
                end_ts = float(stdin_end or c_entry.get("end_ts") or now)
                if c_entry.get("end_ts") != end_ts:
                    c_entry["end_ts"] = end_ts
                    state_dirty = True

            if c_entry.get("start_ts") != start_ts:
                c_entry["start_ts"] = start_ts
                state_dirty = True
            cached_agents[ckey] = c_entry

            agents.append({
                "id": cid or f"stdin:{idx}:{role}",
                "conv_id": cid,
                "role": role,
                "type_name": str(raw.get("type_name") or raw.get("type") or ""),
                "model": stdin_model if stdin_model.lower() != "inherit" else "",
                "description": stdin_desc,
                "active_tool": "",
                "child_last_ts": None,
                "status": norm_status,
                "start_ts": start_ts,
                "end_ts": end_ts,
                "source": "stdin",
            })

    # Reconcile transcript-only running agents omitted from stdin.subagents
    for ai, ag in enumerate(agents):
        if ai in matched_indices or ag.get("status") != "running":
            continue
        start_ts = float(ag.get("start_ts") or now)
        if (now - start_ts) > 2.0:
            ckey = _cache_key(str(ag.get("conv_id") or ""), str(ag.get("role") or ""), str(ag.get("id") or ai))
            c_entry = dict(cached_agents.get(ckey) or {})
            resolved_end = ag.get("child_end_ts") or c_entry.get("end_ts") or now
            ag["status"] = "completed"
            ag["end_ts"] = float(resolved_end)
            if c_entry.get("end_ts") != ag["end_ts"]:
                c_entry["end_ts"] = ag["end_ts"]
                c_entry.setdefault("start_ts", start_ts)
                cached_agents[ckey] = c_entry
                state_dirty = True

    if state_dirty:
        all_state[session_key] = {"updated_at": now, "agents": cached_agents}
        _save_subagent_state(all_state, now)

    return agents


_TODO_MD_RE = re.compile(r"^\s*(?:[-*+]|\d+\.)\s+\[([ xX/])\]\s+(.+)$")


def _parse_markdown_todos(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    todos: list[dict[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    for line in text.splitlines():
        m = _TODO_MD_RE.match(line)
        if not m:
            continue
        mark, raw_content = m.groups()
        content = _sanitize_text(raw_content)
        if not content:
            continue
        if mark in ("x", "X"):
            status = "completed"
        elif mark == "/":
            status = "in_progress"
        else:
            status = "pending"
        todos.append({"content": content, "status": status})
    return todos


def load_todos(data: dict[str, Any], transcript: dict[str, Any], transcript_path: Path | None) -> list[dict[str, str]]:
    brain_dir = _find_brain_dir(data, transcript_path)
    if brain_dir is not None:
        md_todos = _parse_markdown_todos(brain_dir / "task.md")
        if md_todos:
            return md_todos
    return list(transcript.get("todos") or [])


def render_todos_line(todos: list[dict[str, str]], config: dict[str, Any]) -> str:
    display = config["display"]
    colors = config["colors"]
    if not display.get("showTodos", True) or not todos:
        return ""

    total = len(todos)
    completed = sum(1 for td in todos if td.get("status") == "completed")
    in_progress = next((td for td in todos if td.get("status") == "in_progress"), None)

    warn_ansi = resolve_ansi(colors.get("warning"), YELLOW)
    ctx_ansi = resolve_ansi(colors.get("context"), GREEN)

    if in_progress is not None:
        content = _sanitize_text(in_progress.get("content", ""), 50)
        return f"{colorize('▸', warn_ansi)} {content} {c_label(f'({completed}/{total})', colors)}"
    if completed == total and total > 0:
        return f"{colorize('✓', ctx_ansi)} {tr(config, 'status.allTodosComplete')} {c_label(f'({completed}/{total})', colors)}"

    first_pending = next((td for td in todos if td.get("status") == "pending"), None)
    if first_pending is not None:
        content = _sanitize_text(first_pending.get("content", ""), 50)
        return f"{colorize('▸', warn_ansi)} {content} {c_label(f'({completed}/{total})', colors)}"
    return ""


def render_agents_line(agents: list[dict[str, Any]], config: dict[str, Any], now: float) -> str:
    display = config["display"]
    colors = config["colors"]
    if not display.get("showAgents", True):
        return ""
    if not agents:
        return ""

    retention = float(display.get("agentCompletedRetentionSeconds", 60))
    seen_ids: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for a in agents:
        aid = str(a.get("id") or "")
        if aid and aid in seen_ids:
            continue
        if aid:
            seen_ids.add(aid)
        deduped.append(a)

    running = [a for a in deduped if a.get("status") == "running"][-3:]
    rem_slots = max(0, min(2, 3 - len(running)))
    recent_completed = [
        a
        for a in deduped
        if a.get("status") in ("completed", "error")
        and a.get("end_ts") is not None
        and 0 <= (now - float(a["end_ts"])) <= retention
    ][-rem_slots:] if rem_slots > 0 else []

    shown = running + recent_completed
    if not shown:
        return ""

    warn_ansi = resolve_ansi(colors.get("warning"), YELLOW)
    crit_ansi = resolve_ansi(colors.get("critical"), RED)
    ctx_ansi = resolve_ansi(colors.get("context"), GREEN)
    role_ansi = resolve_ansi(colors.get("usageWarning"), MAGENTA)

    lines: list[str] = []
    for ag in shown:
        st = ag.get("status")
        if st == "running":
            icon = colorize("◐", warn_ansi)
        elif st == "error":
            icon = colorize("✗", crit_ansi)
        else:
            icon = colorize("✓", ctx_ansi)

        role_txt = _sanitize_text(ag.get("role") or "subagent", 24) or "subagent"
        role = colorize(role_txt, role_ansi)
        model_short = format_agent_model(str(ag.get("model") or ""))
        mdl = c_label(f" [{model_short}]", colors) if model_short else ""

        raw_desc = str(ag.get("description") or "")
        if not raw_desc and st == "running":
            raw_desc = str(ag.get("active_tool") or "")
        desc_clean = _sanitize_text(raw_desc, 40)
        desc = c_label(f": {desc_clean}", colors) if desc_clean else ""

        end_t = float(ag["end_ts"]) if ag.get("end_ts") is not None else now
        elapsed = format_elapsed_short(end_t - float(ag.get("start_ts") or now))
        lines.append(f"{icon} {role}{mdl}{desc} {c_label(f'({elapsed})', colors)}")
    return "\n".join(lines)


def render_hud(
    data: dict[str, Any],
    config: dict[str, Any] | None = None,
    *,
    now: float | None = None,
    allow_bg_refresh: bool = True,
) -> str:
    cfg = config or load_config()
    now_ts = time.time() if now is None else now
    cwd = str(data.get("cwd") or "")
    agent_state = str(data.get("agent_state") or "idle").lower()

    model_info = data.get("model", {})
    raw_model = (
        str(model_info.get("display_name") or model_info.get("id") or "")
        if isinstance(model_info, dict)
        else str(model_info or "")
    )

    tp = resolve_transcript_path(data)
    transcript = parse_agy_transcript(tp, agent_state=agent_state)
    agents = merge_subagents(data, transcript, now_ts)
    todos = load_todos(data, transcript, tp)
    git_status = get_git_status(cwd, cfg.get("gitStatus", {}), data=data)
    counts = count_agy_configs(cwd)
    stdin_pools = parse_stdin_quota_pools(data, now_ts)
    agy_quota_fallback = (
        {}
        if stdin_pools
        else load_agy_quota(
            raw_model or transcript.get("last_model", ""),
            data,
            allow_bg_refresh=bool(allow_bg_refresh and cfg["display"].get("fallbackLocalServerQuota", False)),
        )
    )

    line1 = render_project_line(data, transcript, git_status, cfg, now_ts)
    ctx_seg = render_context_segment(data, transcript, cfg)
    usage_seg = render_usage_segment(data, stdin_pools, agy_quota_fallback, cfg)
    line2 = f"{ctx_seg} │ {usage_seg}" if (ctx_seg and usage_seg) else (ctx_seg or usage_seg)
    line3 = render_environment_line(counts, cfg)
    line4 = render_tools_line(transcript, cfg)
    line5 = render_agents_line(agents, cfg, now_ts)
    line6 = render_todos_line(todos, cfg)

    raw_lines: list[str] = []
    if cfg.get("lineLayout") == "compact":
        compact_parts = [p for p in (line1, line2, line3) if p]
        if compact_parts:
            raw_lines.append(" │ ".join(compact_parts))
    else:
        for ln in (line1, line2, line3):
            if ln:
                raw_lines.append(ln)

    activity_lines = [ln for ln in (line4, line5, line6) if ln]
    if cfg.get("showSeparators") and raw_lines and activity_lines:
        sep_w = max(20, max(visual_width(ln) for ln in raw_lines))
        raw_lines.append(c_label("─" * sep_w, cfg["colors"]))
    raw_lines.extend(activity_lines)

    term_w = get_terminal_width(cfg, data=data)
    final_lines: list[str] = []
    for ln in raw_lines:
        for sub in ln.split("\n"):
            for wrapped in wrap_line(sub, term_w):
                final_lines.append(f"{RESET}{wrapped}")

    return "\n".join(final_lines)


# ── Main Entrypoint (one-shot + streaming + background quota refresh) ──────────

def _stdin_reader(q: queue.Queue[str | None]) -> None:
    try:
        while True:
            chunk = sys.stdin.read(1)
            if not chunk:
                q.put(None)
                break
            q.put(chunk)
    except Exception:
        q.put(None)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv

    if "--refresh-quota" in args:
        email = ""
        if "--email" in args:
            idx = args.index("--email")
            if idx + 1 < len(args):
                email = args[idx + 1]
        refresh_quota_cache_sync(email)
        return 0

    if os.environ.get("AGY_HUD_DISABLE", "").strip().lower() not in ("", "0", "false", "off", "no"):
        return 0

    config = load_config()
    if sys.stdin is None or sys.stdin.isatty():
        print(tr(config, "init.initializing"), flush=True)
        return 0

    decoder = json.JSONDecoder()
    buf = ""
    rendered_any = False

    q: queue.Queue[str | None] = queue.Queue()
    t = threading.Thread(target=_stdin_reader, args=(q,), daemon=True)
    t.start()

    while True:
        try:
            chunk = q.get(timeout=1.0)
        except queue.Empty:
            continue

        if chunk is None:
            # Flush any trailing valid JSON object on EOF
            stripped = buf.lstrip()
            if stripped:
                try:
                    data, _ = decoder.raw_decode(stripped)
                    if isinstance(data, dict) and data:
                        write_status_state(data)
                        print(render_hud(data, config), flush=True)
                        rendered_any = True
                except json.JSONDecodeError:
                    pass
            if not rendered_any and not buf.strip():
                print(tr(config, "init.initializing"), flush=True)
            break

        buf += chunk
        if chunk == "}":
            while buf:
                stripped = buf.lstrip()
                if not stripped:
                    buf = ""
                    break
                try:
                    data, idx = decoder.raw_decode(stripped)
                    buf = stripped[idx:]
                    if isinstance(data, dict) and any(k in data for k in ("model", "cwd", "context_window", "quota", "agent_state")):
                        write_status_state(data)
                        print(render_hud(data, config), flush=True)
                        rendered_any = True
                except json.JSONDecodeError:
                    break

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
