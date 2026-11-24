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
    },
    "display": {
        "showModel": True,
        "showEffortLevel": True,
        "effortFormat": "full",
        "showProject": True,
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


# ── Git Status ─────────────────────────────────────────────────────────────────

def get_git_status(
    cwd: str,
    git_cfg: dict[str, Any],
    data: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if not git_cfg.get("enabled", True):
        return None

    # Fast path: use native AGY stdin.vcs when provided
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

        if git_cfg.get("showDirty", True):
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

    return {"branch": branch_out, "dirty": dirty, "ahead": ahead, "behind": behind}


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
            plugins_dir = dot_dir / "plugins"
            if plugins_dir.is_dir():
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


def parse_agy_transcript(transcript_path: Path | None, agent_state: str = "idle") -> dict[str, Any]:
    result: dict[str, Any] = {
        "session_start": None,
        "tools": [],
        "agents": [],
        "last_model": "",
        "total_input_tokens": 0,
        "total_cache_read_tokens": 0,
        "total_output_tokens": 0,
    }
    if not transcript_path or not transcript_path.is_file():
        return result

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
        return result

    if not raw_entries:
        return result

    # Deduplicate by step_index (keeping latest) while preserving order
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

    # Collect background task step_indices that later finished or were canceled
    finished_task_steps: set[int] = set()
    for entry in entries:
        content = str(entry.get("content") or "")
        if entry.get("type") == "SYSTEM_MESSAGE" or "task-" in content:
            for m in re.finditer(
                r"task-(\d+)[\"']?\s+(?:was canceled|completed|finished|exited|failed|terminated)",
                content,
                re.IGNORECASE,
            ):
                finished_task_steps.add(int(m.group(1)))
        if entry.get("type") == "PLANNER_RESPONSE":
            for tc in entry.get("tool_calls") or []:
                if isinstance(tc, dict) and tc.get("name") == "manage_task":
                    args = tc.get("args") or {}
                    if _clean_arg_value(args.get("Action", "")).lower() == "kill":
                        tid = _clean_arg_value(args.get("TaskId", ""))
                        mk = re.search(r"task-(\d+)$", tid)
                        if mk:
                            finished_task_steps.add(int(mk.group(1)))

    # Session start from first entry with created_at
    for entry in entries:
        ts = parse_iso_timestamp(entry.get("created_at"))
        if ts is not None:
            result["session_start"] = ts
            break

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

        # Count tool result steps following this planner response
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

            if tc_idx < len(result_steps):
                step = result_steps[tc_idx]
                st = str(step.get("status") or "DONE").upper()
                step_idx = step.get("step_index")
                if st == "RUNNING" and step_idx not in finished_task_steps:
                    status = "running"
                elif st == "ERROR":
                    status = "error"
                else:
                    status = "completed"
                end_ts = parse_iso_timestamp(step.get("created_at"))
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
                role = _clean_arg_value(args.get("Role", "")) or _clean_arg_value(args.get("TypeName", "")) or "subagent"
                model_arg = _clean_arg_value(args.get("Model", ""))
                agent_done = not is_last_batch or agent_state != "working"
                result["agents"].append({
                    "id": f"{p_idx}:{tc_idx}",
                    "role": role,
                    "model": model_arg if model_arg and model_arg != "inherit" else "",
                    "status": "completed" if agent_done else "running",
                    "start_ts": start_ts,
                    "end_ts": end_ts if agent_done else None,
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
        git_seg = f"{c_git('git:(', colors)}{' '.join(inner)}{c_git(')', colors)}"

    if project_seg and git_seg:
        parts.append(f"{project_seg} {git_seg}")
    elif project_seg:
        parts.append(project_seg)
    elif git_seg:
        parts.append(git_seg)

    # 3. Optional sandbox badge
    sandbox_info = data.get("sandbox") or {}
    if display.get("showSandbox", True) and sandbox_info.get("enabled"):
        sb_text = "[sandbox:net]" if sandbox_info.get("allow_network") else "[sandbox]"
        parts.append(colorize(sb_text, ORANGE))

    # 4. Optional non-idle agent state
    if display.get("showAgentState", False):
        st = str(data.get("agent_state") or "idle").lower()
        if st == "working":
            parts.append(colorize("◐ working", CYAN))
        elif st == "waiting":
            parts.append(colorize("⏳ waiting", YELLOW))
        elif st == "error":
            parts.append(colorize("⚠ error", RED))

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
    ctx_val_mode = display.get("contextValue", "percent")
    if ctx_val_mode == "remaining":
        val_text = f"{100 - round(used_pct)}%"
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

    for tool in running[-2:]:
        tname = _format_tool_name(str(tool["name"]), display)
        target = str(tool.get("target") or "")
        target_str = c_label(f": {target}", colors) if target else ""
        parts.append(f"{colorize('◐', YELLOW)} {colorize(tname, CYAN)}{target_str}")

    counts: dict[str, int] = {}
    for tool in completed:
        tname = _format_tool_name(str(tool["name"]), display)
        counts[tname] = counts.get(tname, 0) + 1

    sorted_tools = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    max_vis = int(display.get("toolsMaxVisible", 4))
    visible = sorted_tools if max_vis == 0 else sorted_tools[:max_vis]
    for tname, cnt in visible:
        parts.append(f"{colorize('✓', GREEN)} {tname} {c_label(f'×{cnt}', colors)}")

    hidden = 0 if max_vis == 0 else max(0, len(sorted_tools) - len(visible))
    if hidden > 0:
        parts.append(c_label(f"+{hidden} more", colors))

    return " | ".join(parts)


def render_agents_line(transcript: dict[str, Any], config: dict[str, Any], now: float) -> str:
    display = config["display"]
    colors = config["colors"]
    if not display.get("showAgents", True):
        return ""
    agents = transcript.get("agents") or []
    if not agents:
        return ""

    running = [a for a in agents if a.get("status") == "running"][-3:]
    rem_slots = max(0, min(2, 3 - len(running)))
    recent_completed = [
        a
        for a in agents
        if a.get("status") == "completed"
        and a.get("end_ts") is not None
        and 0 <= (now - float(a["end_ts"])) <= 60.0
    ][-rem_slots:] if rem_slots > 0 else []

    shown = running + recent_completed
    if not shown:
        return ""

    lines: list[str] = []
    for ag in shown:
        icon = colorize("◐", YELLOW) if ag.get("status") == "running" else colorize("✓", GREEN)
        role = colorize(str(ag.get("role") or "subagent")[:24], MAGENTA)
        mdl = c_label(f" [{ag['model']}]", colors) if ag.get("model") else ""
        end_t = float(ag["end_ts"]) if ag.get("end_ts") is not None else now
        elapsed = format_elapsed_short(end_t - float(ag.get("start_ts") or now))
        lines.append(f"{icon} {role}{mdl} {c_label(f'({elapsed})', colors)}")
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
    line5 = render_agents_line(transcript, cfg, now_ts)

    raw_lines: list[str] = []
    if cfg.get("lineLayout") == "compact":
        compact_parts = [p for p in (line1, line2, line3) if p]
        if compact_parts:
            raw_lines.append(" │ ".join(compact_parts))
    else:
        for ln in (line1, line2, line3):
            if ln:
                raw_lines.append(ln)

    activity_lines = [ln for ln in (line4, line5) if ln]
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
