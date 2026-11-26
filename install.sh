#!/usr/bin/env bash
set -euo pipefail

SKIP_SETTINGS=0
for arg in "$@"; do
  case "$arg" in
    --skip-settings)
      SKIP_SETTINGS=1
      ;;
    -h|--help)
      echo "Usage: $0 [--skip-settings]"
      exit 0
      ;;
    *)
      echo "Unknown option: $arg" >&2
      exit 2
      ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_DIR="${AGY_HUD_DIR:-$HOME/.antigravity/agy-hud}"
SETTINGS_FILE="${AGY_SETTINGS_FILE:-$HOME/.gemini/antigravity-cli/settings.json}"
BACKUP_DIR="$INSTALL_DIR/backups"

mkdir -p "$INSTALL_DIR" "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"

if [ "$SKIP_SETTINGS" -eq 0 ] && [ -f "$SETTINGS_FILE" ]; then
  cp "$SETTINGS_FILE" "$BACKUP_DIR/settings.json.$TIMESTAMP.bak"
fi
if [ -f "$INSTALL_DIR/hud.py" ]; then
  cp "$INSTALL_DIR/hud.py" "$BACKUP_DIR/hud.py.$TIMESTAMP.bak"
fi

cp "$SCRIPT_DIR/hud.py" "$INSTALL_DIR/hud.py"
chmod +x "$INSTALL_DIR/hud.py"

TARGET_CONFIG="$INSTALL_DIR/config.json"
SOURCE_CONFIG="$SCRIPT_DIR/config.json"

PYTHON_BIN="$(command -v python3 || true)"
if [ -z "$PYTHON_BIN" ]; then
  for cand in /opt/homebrew/bin/python3 /usr/local/bin/python3 /usr/bin/python3; do
    if [ -x "$cand" ]; then
      PYTHON_BIN="$cand"
      break
    fi
  done
fi
if [ -z "$PYTHON_BIN" ]; then
  echo "Error: python3 not found in PATH or standard locations." >&2
  exit 1
fi

HUD_PY_PATH="$INSTALL_DIR/hud.py"
if [ "$SKIP_SETTINGS" -eq 0 ]; then
  mkdir -p "$(dirname "$SETTINGS_FILE")"
fi

"$PYTHON_BIN" - "$SETTINGS_FILE" "$PYTHON_BIN" "$HUD_PY_PATH" "$SOURCE_CONFIG" "$TARGET_CONFIG" "$SKIP_SETTINGS" <<'PY'
import json
import os
import sys

settings_path, python_exe, hud_path, source_cfg, target_cfg, skip_flag = sys.argv[1:7]
skip_settings = skip_flag == "1"


def quote_if_needed(p: str) -> str:
    return f'"{p}"' if " " in p else p


if not skip_settings:
    try:
        with open(settings_path, "r", encoding="utf-8") as f:
            settings = json.load(f)
        if not isinstance(settings, dict):
            settings = {}
    except FileNotFoundError:
        settings = {}

    settings["statusLine"] = {
        "type": "command",
        "command": f"{quote_if_needed(python_exe)} {quote_if_needed(hud_path)}",
        "enabled": True,
        "padding": 0,
    }
    tmp_settings = settings_path + ".tmp"
    with open(tmp_settings, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp_settings, settings_path)

with open(source_cfg, "r", encoding="utf-8") as f:
    default_cfg = json.load(f)

if os.path.isfile(target_cfg):
    try:
        with open(target_cfg, "r", encoding="utf-8") as f:
            user_cfg = json.load(f)
        if not isinstance(user_cfg, dict):
            user_cfg = {}
    except Exception:
        user_cfg = {}
    git_cfg = user_cfg.setdefault("gitStatus", {})
    if isinstance(git_cfg, dict):
        git_cfg.setdefault("showFileStats", True)
    disp = user_cfg.setdefault("display", {})
    if isinstance(disp, dict):
        for old_key in (
            "showExternalUsage",
            "externalUsagePath",
            "externalUsageFreshnessMs",
            "sevenDayThreshold",
        ):
            disp.pop(old_key, None)
        disp.setdefault("quotaPools", "both")
        disp.setdefault("usageValue", "remaining")
        disp.setdefault("showQuotaBars", False)
        disp.setdefault("showSessionName", True)
        disp.setdefault("showTodos", True)
        disp.setdefault("agentCompletedRetentionSeconds", 60)
    pool_cfg = user_cfg.setdefault("pool", {})
    if isinstance(pool_cfg, dict):
        pool_cfg.setdefault("enabled", True)
        pool_cfg.setdefault("showThreadBadge", True)
        pool_cfg.setdefault("lowQuotaThresholdPercent", 8)
        pool_cfg.setdefault("syncLiveTelemetry", True)
        pool_cfg.setdefault("liveSyncIntervalSeconds", 5)
    # Preserve user-customized colors dict while ensuring colors key exists if absent
    if "colors" not in user_cfg and isinstance(default_cfg.get("colors"), dict):
        user_cfg["colors"] = dict(default_cfg["colors"])
    elif isinstance(user_cfg.get("colors"), dict):
        user_cfg["colors"].setdefault("activePool", "stampRed")
    final_cfg = user_cfg
else:
    final_cfg = default_cfg

tmp_cfg = target_cfg + ".tmp"
with open(tmp_cfg, "w", encoding="utf-8") as f:
    json.dump(final_cfg, f, ensure_ascii=False, indent=2)
    f.write("\n")
os.replace(tmp_cfg, target_cfg)
PY

echo "agy-hud installed successfully!"
echo "  Script  : $HUD_PY_PATH"
echo "  Config  : $TARGET_CONFIG"
if [ "$SKIP_SETTINGS" -eq 0 ]; then
  echo "  Settings: $SETTINGS_FILE"
else
  echo "  Settings: (skipped via --skip-settings)"
fi
echo "  Backups : $BACKUP_DIR"
