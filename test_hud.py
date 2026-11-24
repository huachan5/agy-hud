"""Unit & integration tests for agy-hud."""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

HUD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HUD_DIR))
import hud  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


class TestAgyHud(unittest.TestCase):
    def test_parse_model_and_effort(self):
        self.assertEqual(hud.parse_model_and_effort("Gemini 4 Neon (High)"), ("Gemini 4 Neon", "high"))
        self.assertEqual(hud.parse_model_and_effort("Gemini 4 Hydrogen (Medium)"), ("Gemini 4 Hydrogen", "medium"))
        self.assertEqual(hud.parse_model_and_effort("gemini-4-neon-xhigh"), ("gemini-4-neon", "xhigh"))
        self.assertEqual(hud.parse_model_and_effort("Gemini 4 Pro"), ("Gemini 4 Pro", ""))

    def test_render_full_expanded_hud(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "cspace"
            root.mkdir()
            (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            plugin_dir = root / ".agents" / "plugins" / "cc-compat"
            plugin_dir.mkdir(parents=True)
            (plugin_dir / "plugin.json").write_text('{"name":"cc-compat"}', encoding="utf-8")
            (plugin_dir / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"rea": {"command": "cmd.exe", "args": []}}}),
                encoding="utf-8",
            )
            skill_dir = root / ".agents" / "skills" / "demo-skill"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text("---\nname: demo-skill\ndescription: demo\n---\n", encoding="utf-8")

            # Fake transcript
            transcript = Path(tmp) / "transcript.jsonl"
            now = 1795495000.0
            start_iso = "2026-11-24T03:00:00Z"
            lines = [
                {"step_index": 0, "source": "USER_EXPLICIT", "type": "USER_INPUT", "status": "DONE", "created_at": start_iso},
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": start_iso,
                    "model": "gemini-4-neon-high",
                    "input_tokens": 12000,
                    "output_tokens": 500,
                    "tool_calls": [
                        {"name": "run_command", "args": {"CommandLine": "pytest", "toolSummary": "Run tests"}},
                        {"name": "read_file", "args": {"Path": "C:/a/b.py"}},
                        {"name": "read_file", "args": {"Path": "C:/a/c.py"}},
                        {"name": "edit_file", "args": {"Path": "C:/a/b.py"}},
                        {"name": "write_file", "args": {"Path": "C:/a/d.py"}},
                        {"name": "search_web", "args": {"query": "agy"}},
                    ],
                },
                *({"step_index": i + 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": start_iso} for i in range(6)),
            ]
            transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

            # Fake AGY quota cache & Codex external usage
            quota_file = Path(tmp) / "quota-cache.json"
            quota_file.write_text(
                json.dumps({
                    "timestamp": now - 10,
                    "scope": {"email": "test@example.com", "plan_tier": "Pro"},
                    "models": {
                        "gemini4neonhigh": {
                            "name": "Gemini 4 Neon (High)",
                            "remaining_percentage": 85.0,
                            "reset_time": "2026-11-24T08:00:00Z",
                        }
                    },
                }),
                encoding="utf-8",
            )
            ext_file = Path(tmp) / "codex-usage.json"
            ext_file.write_text(
                json.dumps({
                    "five_hour": {"used_percentage": 30, "resets_at": "2026-11-24T08:00:00Z"},
                    "seven_day": {"used_percentage": 98, "resets_at": "2026-11-25T14:00:00Z"},
                    "updated_at": now - 30,
                    "balance_label": "Codex",
                }),
                encoding="utf-8",
            )

            old_qc = hud.QUOTA_CACHE_FILE
            try:
                hud.QUOTA_CACHE_FILE = quota_file
                cfg = hud._deep_merge(
                    hud.DEFAULT_CONFIG,
                    {"display": {"externalUsagePath": str(ext_file), "showExternalUsage": True}},
                )
                payload = {
                    "model": {"display_name": "Gemini 4 Neon (High)", "id": "gemini-4-neon-high"},
                    "agent_state": "idle",
                    "context_window": {"remaining_percentage": 93.0, "total_input_tokens": 12000, "total_output_tokens": 500},
                    "cwd": str(root),
                    "email": "test@example.com",
                    "plan_tier": "Google AI Pro",
                    "transcript_path": str(transcript),
                }
                rendered = hud.render_hud(payload, cfg, now=now, allow_bg_refresh=False)
                plain = strip_ansi(rendered)
                rows = plain.splitlines()
                self.assertEqual(len(rows), 4, f"Expected 4 rows, got:\n{plain}")
                self.assertIn("[Gemini 4 Neon ◑ high]", rows[0])
                self.assertIn("cspace", rows[0])
                self.assertIn("⏱ ", rows[0])
                self.assertIn("上下文", rows[1])
                self.assertIn("7%", rows[1])
                self.assertIn("AGY余: 85%", rows[1])
                self.assertIn("5h: 30%", rows[1])
                self.assertIn("7d: 98%", rows[1])
                self.assertIn("Codex", rows[1])
                self.assertIn("1 AGENTS.md", rows[2])
                self.assertIn("1 MCPs", rows[2])
                self.assertIn("1 Skills", rows[2])
                self.assertIn("✓ Read ×2", rows[3])
                self.assertIn("+1 more", rows[3])
            finally:
                hud.QUOTA_CACHE_FILE = old_qc

    def test_oneshot_cli_execution(self):
        payload = json.dumps({
            "model": {"display_name": "Gemini 4 Hydrogen (Medium)"},
            "agent_state": "idle",
            "context_window": {"remaining_percentage": 82.0},
            "cwd": str(HUD_DIR.parent),
        })
        proc = subprocess.run(
            [sys.executable, str(HUD_DIR / "hud.py")],
            input=payload,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=5,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        plain = strip_ansi(proc.stdout)
        self.assertIn("[Gemini 4 Hydrogen ◔ medium]", plain)
        self.assertIn("cspace", plain)
        self.assertIn("上下文", plain)
        self.assertIn("18%", plain)
        self.assertIn("AGENTS.md", plain)
        self.assertIn("MCPs", plain)

    def test_canceled_background_task_not_stuck_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "transcript.jsonl"
            lines = [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-24T03:00:00Z",
                    "tool_calls": [{"name": "run_command", "args": {"toolSummary": "\"Long task\""}}],
                },
                {"step_index": 29, "source": "MODEL", "type": "GENERIC", "status": "RUNNING", "created_at": "2026-11-24T03:00:01Z"},
                {
                    "step_index": 30,
                    "source": "SYSTEM",
                    "type": "SYSTEM_MESSAGE",
                    "status": "DONE",
                    "created_at": "2026-11-24T03:00:05Z",
                    "content": 'Task id "abc/task-29" was canceled with result:',
                },
            ]
            transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            parsed = hud.parse_agy_transcript(transcript, agent_state="idle")
            self.assertEqual(len(parsed["tools"]), 1)
            self.assertEqual(parsed["tools"][0]["status"], "completed")


if __name__ == "__main__":
    unittest.main()
