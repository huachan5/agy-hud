"""Unit & integration tests for agy-hud."""
from __future__ import annotations

import json
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HUD_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(HUD_DIR))
import hud  # noqa: E402

ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(s: str) -> str:
    return ANSI_RE.sub("", s)


SAMPLE_QUOTA = {
    "gemini-5h": {"remaining_fraction": 0.92, "reset_in_seconds": 15120},
    "gemini-weekly": {"remaining_fraction": 0.88, "reset_in_seconds": 442800},
    "3p-5h": {"remaining_fraction": 0.75, "reset_in_seconds": 9600},
    "3p-weekly": {"remaining_fraction": 0.81, "reset_in_seconds": 522000},
}


class TestAgyHud(unittest.TestCase):
    def test_parse_model_and_effort(self):
        self.assertEqual(hud.parse_model_and_effort("Gemini 4 Neon (High)"), ("Gemini 4 Neon", "high"))
        self.assertEqual(hud.parse_model_and_effort("Gemini 4 Hydrogen (Medium)"), ("Gemini 4 Hydrogen", "medium"))
        self.assertEqual(hud.parse_model_and_effort("gemini-4-neon-xhigh"), ("gemini-4-neon", "xhigh"))
        self.assertEqual(hud.parse_model_and_effort("Gemini 4 Pro"), ("Gemini 4 Pro", ""))

    def test_dual_quota_pools_gemini_active(self):
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

            payload = {
                "model": {"display_name": "Gemini 4 Neon (High)", "id": "gemini-4-neon-high"},
                "agent_state": "idle",
                "context_window": {"remaining_percentage": 93.0, "total_input_tokens": 12000, "total_output_tokens": 500},
                "cwd": str(root),
                "vcs": {"branch": "feat/hud", "dirty": True, "type": "git"},
                "terminal_width": 220,
                "quota": SAMPLE_QUOTA,
                "plan_tier": "Google AI Pro",
                "transcript_path": str(transcript),
            }
            with mock.patch.object(hud, "HOME_DIR", Path(tmp)):
                rendered = hud.render_hud(payload, hud.DEFAULT_CONFIG, now=now, allow_bg_refresh=False)
            plain = strip_ansi(rendered)
            rows = plain.splitlines()
            self.assertEqual(len(rows), 6, f"Expected 6 rows, got:\n{plain}")
            self.assertIn("[Gemini 4 Neon ◑ high]", rows[0])
            self.assertIn("cspace git:(feat/hud*)", rows[0])
            self.assertIn("上下文", rows[1])
            self.assertIn("7%", rows[1])
            self.assertEqual(rows[2], " [Gemini]  5h: 92% (4h 12m)  7d: 88% (5d 3h) ")
            self.assertEqual(rows[3], " [3P]      5h: 75% (2h 40m)  7d: 81% (6d 1h)")
            # Active pool row has continuous slate row background, inactive pool tag is dim
            self.assertIn(f"{hud.SLATE_ROW_BG} \x1b[1;97m[Gemini]", rendered)
            self.assertIn(f"{hud.DIM}[3P]{hud.RESET}", rendered)
            self.assertNotIn("Codex", plain)
            self.assertIn("1 AGENTS.md", rows[4])
            self.assertIn("1 MCPs", rows[4])
            self.assertIn("1 Skills", rows[4])
            self.assertIn("✓ Read ×2", rows[5])
            self.assertIn("+1 more", rows[5])

    def test_3p_model_orders_3p_first_and_active_only_mode(self):
        payload = {
            "model": {"display_name": "Claude Sonnet 4.6", "id": "claude-sonnet-4-6"},
            "context_window": {"used_percentage": 25.0},
            "quota": SAMPLE_QUOTA,
        }
        both_rendered = hud.render_hud(payload, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False)
        both_plain = strip_ansi(both_rendered)
        self.assertLess(both_plain.index(" [3P] "), both_plain.index(" [Gemini] "))
        self.assertIn(f"{hud.SLATE_ROW_BG} \x1b[1;97m[3P]", both_rendered)
        self.assertIn(f"{hud.DIM}[Gemini]{hud.RESET}", both_rendered)

        cfg_active = hud._deep_merge(hud.DEFAULT_CONFIG, {"display": {"quotaPools": "active"}})
        active_plain = strip_ansi(hud.render_hud(payload, cfg_active, now=1000.0, allow_bg_refresh=False))
        self.assertIn(" [3P]  5h: 75%", active_plain)
        self.assertNotIn("[Gemini]", active_plain)

    def test_usage_value_used_mode(self):
        payload = {
            "model": {"display_name": "Gemini 4 Pro", "id": "gemini-4-pro"},
            "context_window": {"used_percentage": 10.0},
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.70, "reset_in_seconds": 3600},
            },
        }
        cfg_used = hud._deep_merge(hud.DEFAULT_CONFIG, {"display": {"usageValue": "used"}})
        plain = strip_ansi(hud.render_hud(payload, cfg_used, now=1000.0, allow_bg_refresh=False))
        self.assertIn(" [Gemini]  5h: 30% (1h)", plain)

    def test_oneshot_cli_execution(self):
        payload = json.dumps({
            "model": {"display_name": "Gemini 4 Hydrogen (Medium)"},
            "agent_state": "idle",
            "context_window": {"remaining_percentage": 82.0},
            "cwd": str(HUD_DIR.parent),
            "quota": SAMPLE_QUOTA,
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
        self.assertIn(HUD_DIR.parent.name, plain)
        self.assertIn("上下文", plain)
        self.assertIn("18%", plain)
        self.assertIn(" [Gemini]  5h: 92%", plain)
        self.assertIn(" [3P]      5h: 75%", plain)
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

    def test_no_window_suppression_on_windows(self):
        kwargs = hud._no_window_kwargs()
        if sys.platform == "win32":
            self.assertEqual(kwargs, {"creationflags": subprocess.CREATE_NO_WINDOW})
        src = (HUD_DIR / "hud.py").read_text(encoding="utf-8")
        self.assertNotIn("DETACHED_PROCESS", src)

    def test_background_subagent_stays_running_on_idle_and_subsequent_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_sub_state = hud.SUBAGENT_STATE_FILE
            old_sess_starts = hud.SESSION_STARTS_FILE
            hud.SUBAGENT_STATE_FILE = Path(tmp) / "subagent-state.json"
            hud.SESSION_STARTS_FILE = Path(tmp) / "session-starts.json"
            try:
                child_cid = "11111111-2222-3333-4444-555555555555"
                transcript = Path(tmp) / "transcript.jsonl"
                lines = [
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:00Z",
                        "tool_calls": [
                            {
                                "name": "invoke_subagent",
                                "args": {
                                    "Role": "Codebase Researcher",
                                    "TypeName": "research",
                                    "Model": "inherit",
                                    "Prompt": "Find auth middleware implementation details",
                                },
                            }
                        ],
                    },
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:01Z",
                        "content": f"Subagent launched with conversationId: {child_cid}",
                    },
                    {
                        "step_index": 3,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:02Z",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "C:/repo/main.py"}}],
                    },
                    {
                        "step_index": 4,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:03Z",
                        "content": "file contents",
                    },
                ]
                transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

                now = hud.parse_iso_timestamp("2026-11-24T08:00:15Z")
                payload = {
                    "model": {"display_name": "Gemini 4 Neon (High)"},
                    "agent_state": "idle",
                    "context_window": {"used_percentage": 15.0},
                    "transcript_path": str(transcript),
                }
                plain = strip_ansi(hud.render_hud(payload, hud.DEFAULT_CONFIG, now=now, allow_bg_refresh=False))
                self.assertIn("◐ Codebase Researcher: Find auth middleware implementation det… (15s)", plain)
            finally:
                hud.SUBAGENT_STATE_FILE = old_sub_state
                hud.SESSION_STARTS_FILE = old_sess_starts

    def test_stdin_subagents_merge_with_transcript_and_child_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_sub_state = hud.SUBAGENT_STATE_FILE
            old_sess_starts = hud.SESSION_STARTS_FILE
            hud.SUBAGENT_STATE_FILE = Path(tmp) / "subagent-state.json"
            hud.SESSION_STARTS_FILE = Path(tmp) / "session-starts.json"
            try:
                brain_root = Path(tmp) / "brain"
                parent_cid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
                child_cid = "99999999-8888-7777-6666-555555555555"

                parent_log = brain_root / parent_cid / ".system_generated" / "logs" / "transcript.jsonl"
                parent_log.parent.mkdir(parents=True)
                parent_lines = [
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:00Z",
                        "tool_calls": [
                            {
                                "name": "invoke_subagent",
                                "args": {
                                    "Role": "Codebase Researcher",
                                    "Model": "inherit",
                                    "Prompt": "### Search auth middleware\nCheck JWT verification",
                                },
                            }
                        ],
                    },
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:01Z",
                        "content": f'{{"conversationId": "{child_cid}"}}',
                    },
                ]
                parent_log.write_text("\n".join(json.dumps(x) for x in parent_lines) + "\n", encoding="utf-8")

                child_log = brain_root / child_cid / ".system_generated" / "logs" / "transcript.jsonl"
                child_log.parent.mkdir(parents=True)
                child_lines = [
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:01Z",
                        "model": "gemini-4-hydrogen-medium",
                        "tool_calls": [{"name": "read_file", "args": {"Path": "C:/repo/auth.py"}}],
                    }
                ]
                child_log.write_text("\n".join(json.dumps(x) for x in child_lines) + "\n", encoding="utf-8")

                t1 = hud.parse_iso_timestamp("2026-11-24T08:00:13Z")
                payload_running = {
                    "conversation_id": parent_cid,
                    "model": {"display_name": "Gemini 4 Neon (High)"},
                    "agent_state": "idle",
                    "context_window": {"used_percentage": 20.0},
                    "transcript_path": str(parent_log),
                    "subagents": [
                        {"conversation_id": child_cid, "role": "Codebase Researcher", "status": "running"}
                    ],
                }
                plain_running = strip_ansi(hud.render_hud(payload_running, hud.DEFAULT_CONFIG, now=t1, allow_bg_refresh=False))
                self.assertIn("◐ Codebase Researcher [gemini-4-hydrogen]: Search auth middleware (12s)", plain_running)

                # Explicit completed status in stdin.subagents
                t2 = hud.parse_iso_timestamp("2026-11-24T08:00:21Z")
                payload_done = dict(payload_running)
                payload_done["subagents"] = [
                    {"conversation_id": child_cid, "role": "Codebase Researcher", "status": "completed"}
                ]
                plain_done = strip_ansi(hud.render_hud(payload_done, hud.DEFAULT_CONFIG, now=t2, allow_bg_refresh=False))
                self.assertIn("✓ Codebase Researcher [gemini-4-hydrogen]: Search auth middleware (20s)", plain_done)

                # Still retained within 60s even if stdin.subagents becomes empty []
                t3 = hud.parse_iso_timestamp("2026-11-24T08:00:40Z")
                payload_empty = dict(payload_running)
                payload_empty["subagents"] = []
                plain_retained = strip_ansi(hud.render_hud(payload_empty, hud.DEFAULT_CONFIG, now=t3, allow_bg_refresh=False))
                self.assertIn("✓ Codebase Researcher [gemini-4-hydrogen]: Search auth middleware (20s)", plain_retained)

                # Expired after > 60s retention
                t4 = hud.parse_iso_timestamp("2026-11-24T08:01:25Z")
                plain_expired = strip_ansi(hud.render_hud(payload_empty, hud.DEFAULT_CONFIG, now=t4, allow_bg_refresh=False))
                self.assertNotIn("Codebase Researcher", plain_expired)
            finally:
                hud.SUBAGENT_STATE_FILE = old_sub_state
                hud.SESSION_STARTS_FILE = old_sess_starts

    def test_subagent_kill_and_swarm_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_sub_state = hud.SUBAGENT_STATE_FILE
            old_sess_starts = hud.SESSION_STARTS_FILE
            hud.SUBAGENT_STATE_FILE = Path(tmp) / "subagent-state.json"
            hud.SESSION_STARTS_FILE = Path(tmp) / "session-starts.json"
            try:
                c1 = "11111111-1111-1111-1111-111111111111"
                c2 = "22222222-2222-2222-2222-222222222222"
                transcript = Path(tmp) / "transcript.jsonl"
                lines = [
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:00Z",
                        "tool_calls": [
                            {"name": "invoke_subagent", "args": {"Role": "Agent One", "Prompt": "Task 1"}},
                            {"name": "invoke_subagent", "args": {"Role": "Agent Two", "Prompt": "Task 2"}},
                            {"name": "swarm", "args": {"action": "add", "name": "worker", "model": "claude-haiku-4-5-20251001", "tasks": ["Lint files", "Run unit tests"]}},
                        ],
                    },
                    {"step_index": 2, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-24T08:00:01Z", "content": f"id={c1}"},
                    {"step_index": 3, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-24T08:00:01Z", "content": f"id={c2}"},
                    {"step_index": 4, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-24T08:00:01Z", "content": "spawned"},
                    {
                        "step_index": 5,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-24T08:00:10Z",
                        "tool_calls": [
                            {"name": "manage_subagents", "args": {"Action": "kill", "ConversationIds": [c1]}},
                            {"name": "swarm", "args": {"action": "kill", "name": "worker-1"}},
                        ],
                    },
                    {"step_index": 6, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-24T08:00:11Z", "content": "killed"},
                    {"step_index": 7, "source": "MODEL", "type": "GENERIC", "status": "DONE", "created_at": "2026-11-24T08:00:11Z", "content": "killed"},
                ]
                transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

                now = hud.parse_iso_timestamp("2026-11-24T08:00:25Z")
                parsed = hud.parse_agy_transcript(transcript, agent_state="idle")
                by_role = {a["role"]: a for a in parsed["agents"]}
                self.assertEqual(by_role["Agent One"]["status"], "completed")
                self.assertEqual(by_role["Agent Two"]["status"], "running")
                self.assertEqual(by_role["worker-1"]["status"], "completed")
                self.assertEqual(by_role["worker-2"]["status"], "running")

                plain = strip_ansi(hud.render_hud(
                    {"model": {"display_name": "Gemini 4 Neon"}, "context_window": {"used_percentage": 10}, "transcript_path": str(transcript)},
                    hud.DEFAULT_CONFIG,
                    now=now,
                    allow_bg_refresh=False,
                ))
                self.assertIn("◐ Agent Two: Task 2 (25s)", plain)
                self.assertIn("◐ worker-2 [haiku-4.5]: Run unit tests (25s)", plain)
                self.assertIn("✓ worker-1 [haiku-4.5]: Lint files (10s)", plain)

                # Test kill_all
                lines.append({
                    "step_index": 8,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-24T08:00:30Z",
                    "tool_calls": [{"name": "manage_subagents", "args": {"Action": "kill_all"}}],
                })
                transcript.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
                parsed2 = hud.parse_agy_transcript(transcript, agent_state="idle")
                by_role2 = {a["role"]: a for a in parsed2["agents"]}
                self.assertEqual(by_role2["Agent Two"]["status"], "completed")
            finally:
                hud.SUBAGENT_STATE_FILE = old_sub_state
                hud.SESSION_STARTS_FILE = old_sess_starts

    def test_session_title_and_context_value_modes(self):
        base_payload = {
            "model": {"display_name": "Gemini 4 Neon (High)"},
            "conversation_title": "这是一个非常非常长长长长长长长长长长长长长长长长长长的测试会话标题",
            "context_window": {
                "used_percentage": 45.0,
                "context_window_size": 200000,
                "current_usage": {"input_tokens": 60000, "cache_read_input_tokens": 30000},
            },
        }
        plain_pct = strip_ansi(hud.render_hud(base_payload, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False))
        line1 = plain_pct.splitlines()[0]
        self.assertIn("…", line1)
        self.assertIn("45%", plain_pct)

        cfg_rem = hud._deep_merge(hud.DEFAULT_CONFIG, {"display": {"contextValue": "remaining"}})
        self.assertIn("55%", strip_ansi(hud.render_hud(base_payload, cfg_rem, now=1000.0, allow_bg_refresh=False)))

        cfg_tok = hud._deep_merge(hud.DEFAULT_CONFIG, {"display": {"contextValue": "tokens"}})
        self.assertIn("90k/200k", strip_ansi(hud.render_hud(base_payload, cfg_tok, now=1000.0, allow_bg_refresh=False)))

        cfg_both = hud._deep_merge(hud.DEFAULT_CONFIG, {"display": {"contextValue": "both"}})
        self.assertIn("45% (90k/200k)", strip_ansi(hud.render_hud(base_payload, cfg_both, now=1000.0, allow_bg_refresh=False)))

    def test_task_md_todos_rendering(self):
        with tempfile.TemporaryDirectory() as tmp:
            old_sess_starts = hud.SESSION_STARTS_FILE
            hud.SESSION_STARTS_FILE = Path(tmp) / "session-starts.json"
            try:
                conv_dir = Path(tmp) / "brain" / "conv-todo"
                log_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                log_path.parent.mkdir(parents=True)
                log_path.write_text('{"step_index": 0, "created_at": "2026-11-24T08:00:00Z"}\n', encoding="utf-8")

                task_md = conv_dir / "task.md"
                task_md.write_text(
                    "- [x] 设计数据结构\n- [/] 实现子 Agent 追踪\n- [ ] 编写单元测试\n",
                    encoding="utf-8",
                )
                payload = {
                    "model": {"display_name": "Gemini 4 Neon"},
                    "context_window": {"used_percentage": 12.0},
                    "transcript_path": str(log_path),
                }
                plain_ip = strip_ansi(hud.render_hud(payload, hud.DEFAULT_CONFIG, now=hud.parse_iso_timestamp("2026-11-24T08:05:00Z"), allow_bg_refresh=False))
                self.assertIn("▸ 实现子 Agent 追踪 (1/3)", plain_ip)

                # All completed (zh-Hans and en)
                task_md.write_text("- [x] A\n- [x] B\n", encoding="utf-8")
                plain_done_zh = strip_ansi(hud.render_hud(payload, hud.DEFAULT_CONFIG, now=hud.parse_iso_timestamp("2026-11-24T08:05:00Z"), allow_bg_refresh=False))
                self.assertIn("✓ 所有任务已完成 (2/2)", plain_done_zh)

                cfg_en = hud._deep_merge(hud.DEFAULT_CONFIG, {"language": "en"})
                plain_done_en = strip_ansi(hud.render_hud(payload, cfg_en, now=hud.parse_iso_timestamp("2026-11-24T08:05:00Z"), allow_bg_refresh=False))
                self.assertIn("✓ All todos complete (2/2)", plain_done_en)

                # Pending only
                task_md.write_text("- [x] A\n- [ ] 待办事项 B\n", encoding="utf-8")
                plain_pending = strip_ansi(hud.render_hud(payload, hud.DEFAULT_CONFIG, now=hud.parse_iso_timestamp("2026-11-24T08:05:00Z"), allow_bg_refresh=False))
                self.assertIn("▸ 待办事项 B (1/2)", plain_pending)
            finally:
                hud.SESSION_STARTS_FILE = old_sess_starts

    def test_git_file_stats_parsing_and_rendering(self):
        parsed = hud._parse_porcelain_file_stats(" M keep.txt\nA  added.txt\n D del.txt\n?? untracked.txt\nR  old.txt -> new.txt\n")
        self.assertEqual(parsed, {"modified": 2, "added": 1, "deleted": 1, "untracked": 1})

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "demo-repo"
            repo.mkdir()
            porcelain_out = " M keep.txt\nA  added.txt\n D del.txt\n?? untracked.txt\n"

            payload = {
                "model": {"display_name": "Gemini 4 Neon"},
                "cwd": str(repo),
                "vcs": {"branch": "main", "dirty": False},
                "context_window": {"used_percentage": 10.0},
            }

            cfg_on = hud._deep_merge(hud.DEFAULT_CONFIG, {"gitStatus": {"showFileStats": True}})
            with mock.patch.object(hud.subprocess, "check_output", return_value=porcelain_out) as mock_run:
                plain_on = strip_ansi(hud.render_hud(payload, cfg_on, now=1000.0, allow_bg_refresh=False))
                self.assertEqual(mock_run.call_count, 1)
                self.assertRegex(plain_on, r"git:\(main\* !1 \+1 ✘1 \?1\)")

            cfg_off = hud._deep_merge(hud.DEFAULT_CONFIG, {"gitStatus": {"showFileStats": False}})
            with mock.patch.object(hud.subprocess, "check_output") as mock_run_off:
                plain_off = strip_ansi(hud.render_hud(payload, cfg_off, now=1000.0, allow_bg_refresh=False))
                self.assertEqual(mock_run_off.call_count, 0)
                self.assertIn("git:(main)", plain_off)

    def test_global_plugins_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            plugins_root = tmp_path / ".gemini" / "config" / "plugins"
            enabled_plugin = plugins_root / "migrated-settings"
            (enabled_plugin / "skills" / "foo").mkdir(parents=True)
            (enabled_plugin / "skills" / "foo" / "SKILL.md").write_text("---\nname: foo\n---\n", encoding="utf-8")
            (enabled_plugin / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"rea": {}, "bosskit": {}}}),
                encoding="utf-8",
            )
            disabled_plugin = plugins_root / "disabled-plugin"
            (disabled_plugin / "skills" / "bar").mkdir(parents=True)
            (disabled_plugin / "skills" / "bar" / "SKILL.md").write_text("---\nname: bar\n---\n", encoding="utf-8")
            (disabled_plugin / "mcp_config.json").write_text(
                json.dumps({"mcpServers": {"ignored_mcp": {}}}),
                encoding="utf-8",
            )
            (disabled_plugin / "plugin.json").write_text(json.dumps({"disabled": True}), encoding="utf-8")

            work_dir = tmp_path / "workspace"
            work_dir.mkdir()
            with mock.patch.object(hud, "HOME_DIR", tmp_path):
                counts = hud.count_agy_configs(str(work_dir))
            self.assertEqual(counts["mcpCount"], 2)
            self.assertEqual(counts["skillsCount"], 1)

    def test_custom_claude_hud_colors_applied_across_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "claudespace"
            root.mkdir()
            conv_dir = tmp_path / "conv"
            log_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            log_path.parent.mkdir(parents=True)
            lines = [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-24T08:00:00Z",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/tmp/a.py"}},
                        {"name": "run_command", "args": {"CommandLine": "pytest"}},
                    ],
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-24T08:00:01Z",
                },
            ]
            log_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")
            (conv_dir / "task.md").write_text("- [x] A\n- [x] B\n", encoding="utf-8")

            claude_colors = {
                "model": "#F92672",
                "project": "#10B981",
                "git": "#10B981",
                "gitBranch": "#F92672",
                "context": "#10B981",
                "usage": "#005FCC",
                "usageWarning": "brightMagenta",
                "warning": "yellow",
                "critical": "red",
                "label": "dim",
                "custom": 208,
                "barFilled": "█",
                "barEmpty": "░",
            }
            cfg = hud._deep_merge(hud.DEFAULT_CONFIG, {"colors": claude_colors})
            payload = {
                "model": {"display_name": "Gemini 4 Neon (XHigh)", "id": "gemini-4-neon"},
                "agent_state": "working",
                "cwd": str(root),
                "vcs": {"branch": "main", "dirty": True},
                "sandbox": {"enabled": True, "allow_network": False},
                "context_window": {"used_percentage": 20.0},
                "quota": SAMPLE_QUOTA,
                "transcript_path": str(log_path),
                "subagents": [
                    {
                        "conversation_id": "sub-1",
                        "role": "Researcher",
                        "model": "gemini-4-flash",
                        "status": "completed",
                        "started_at": "2026-11-24T08:00:00Z",
                        "ended_at": "2026-11-24T08:00:03Z",
                    }
                ],
            }
            with mock.patch.object(hud, "HOME_DIR", tmp_path):
                rendered = hud.render_hud(
                    payload,
                    cfg,
                    now=hud.parse_iso_timestamp("2026-11-24T08:00:05Z"),
                    allow_bg_refresh=False,
                )

            pink = "\x1b[38;2;249;38;114m"
            emerald = "\x1b[38;2;16;185;129m"
            cobalt = "\x1b[38;2;0;95;204m"
            orange208 = "\x1b[38;5;208m"
            self.assertIn(f"{pink}[Gemini 4 Neon ◕ xhigh]", rendered)
            self.assertIn(f"{emerald}claudespace", rendered)
            self.assertIn(f"{emerald}git:(", rendered)
            self.assertIn(f"{pink}main*", rendered)
            self.assertIn(f"{orange208}[sandbox]", rendered)
            self.assertIn(f"{emerald}20%", rendered)
            self.assertIn(f"{cobalt}92%", rendered)
            self.assertIn(f"{cobalt}Shell", rendered)
            self.assertIn(f"{emerald}✓\x1b[0m Read", rendered)
            self.assertIn(f"{emerald}✓\x1b[0m \x1b[95mResearcher", rendered)
            self.assertIn(f"{emerald}✓\x1b[0m 所有任务已完成", rendered)

    @staticmethod
    def _create_pool_db(db_path: Path, *, main_hash: str, backup_row: dict | None = None) -> None:
        import sqlite3

        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE accounts (
                    account_id TEXT PRIMARY KEY,
                    label TEXT,
                    email_hint TEXT,
                    email_hash TEXT UNIQUE,
                    status TEXT NOT NULL,
                    gemini_5h_remaining REAL,
                    gemini_5h_reset_at TEXT,
                    gemini_7d_remaining REAL,
                    quota_updated_at TEXT,
                    cooldown_until TEXT
                );
                CREATE TABLE threads (
                    thread_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    cwd TEXT NOT NULL,
                    anchor_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_segment_id TEXT,
                    segment_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE segments (
                    segment_id TEXT PRIMARY KEY,
                    thread_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    account_id TEXT NOT NULL,
                    parent_segment_id TEXT,
                    conversation_id TEXT,
                    transcript_path TEXT,
                    handoff_in_path TEXT,
                    handoff_out_path TEXT,
                    pid INTEGER,
                    pid_create_time INTEGER,
                    status TEXT NOT NULL,
                    observed_email_hint TEXT,
                    observed_email_hash TEXT,
                    started_at TEXT NOT NULL,
                    ended_at TEXT,
                    last_heartbeat_at TEXT
                );
                """
            )
            conn.execute(
                "INSERT INTO accounts(account_id, label, email_hint, email_hash, status) VALUES (?,?,?,?,?)",
                ("main", "主力", "m***@example.test", main_hash, "active"),
            )
            if backup_row is not None:
                conn.execute(
                    "INSERT INTO accounts(account_id, label, email_hint, email_hash, status, "
                    "gemini_5h_remaining, gemini_5h_reset_at, gemini_7d_remaining, quota_updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        "backup",
                        "备用",
                        "b***@example.test",
                        backup_row["email_hash"],
                        "ready",
                        backup_row.get("gemini_5h_remaining"),
                        backup_row.get("gemini_5h_reset_at"),
                        backup_row.get("gemini_7d_remaining"),
                        backup_row.get("quota_updated_at"),
                    ),
                )
            conn.execute(
                "INSERT INTO threads VALUES ('T1','任务','/repo','anchors/T1.md','active','T1-seg1',1,'2026-11-26T00:00:00Z','2026-11-26T00:00:00Z')"
            )
            conn.execute(
                "INSERT INTO segments(segment_id, thread_id, seq, account_id, status, started_at) "
                "VALUES ('T1-seg1','T1',1,'main','running','2026-11-26T00:00:00Z')"
            )
            conn.commit()
        finally:
            conn.close()

    def test_argon_merged_into_gemini_quota_pool(self):
        # 1. Legacy argon key appears BEFORE standard gemini-weekly -> standard key still wins
        quota_legacy_first = {
            "gemini-argon-wtu-weekly": {"remaining_fraction": 0.84, "reset_in_seconds": 439200},
            **SAMPLE_QUOTA,
            "gemini-weekly": {"remaining_fraction": 0.88, "reset_in_seconds": 442800},
            "3p-5h": {"remaining_fraction": 0.50, "reset_in_seconds": 3600, "disabled": True},
            "3p-weekly": {"remaining_fraction": 0.50, "reset_in_seconds": 3600, "disabled": True},
        }
        # 2. Legacy argon key appears AFTER standard gemini-weekly -> standard key still wins
        quota_std_first = {
            **SAMPLE_QUOTA,
            "gemini-weekly": {"remaining_fraction": 0.88, "reset_in_seconds": 442800},
            "gemini-argon-wtu-weekly": {"remaining_fraction": 0.84, "reset_in_seconds": 439200},
            "3p-5h": {"remaining_fraction": 0.50, "reset_in_seconds": 3600, "disabled": True},
            "3p-weekly": {"remaining_fraction": 0.50, "reset_in_seconds": 3600, "disabled": True},
        }

        for quota in (quota_legacy_first, quota_std_first):
            payload_argon = {
                "model": {"display_name": "Gemini 4 Argon (High)", "id": "gemini-4-argon-high"},
                "context_window": {"used_percentage": 15.0},
                "quota": quota,
            }
            plain_argon = strip_ansi(hud.render_hud(payload_argon, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False))
            rows_argon = plain_argon.splitlines()
            self.assertIn(" [Gemini]  5h: 92%", rows_argon[2])
            self.assertIn("7d: 88%", rows_argon[2])
            self.assertNotIn("[Argon]", plain_argon)
            self.assertNotIn("[3P]", plain_argon)

        # 3. Active 3P + legacy argon key + Neon model -> strictly 2 pool rows ([Gemini], [3P])
        payload_neon = {
            "model": {"display_name": "Gemini 4 Neon (High)", "id": "gemini-4-neon-high"},
            "context_window": {"used_percentage": 15.0},
            "quota": {**SAMPLE_QUOTA, "gemini-argon-wtu-weekly": {"remaining_fraction": 0.84, "reset_in_seconds": 439200}},
        }
        plain_neon = strip_ansi(hud.render_hud(payload_neon, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False))
        rows_neon = plain_neon.splitlines()
        self.assertTrue(rows_neon[2].startswith(" [Gemini]"))
        self.assertTrue(rows_neon[3].startswith(" [3P]"))
        self.assertNotIn("[Argon]", plain_neon)

        # 4. Only legacy argon weekly present (no gemini-weekly) -> merged into Gemini 7d slot
        payload_only_legacy = {
            "model": {"display_name": "Gemini 4 Argon (High)", "id": "gemini-4-argon-high"},
            "context_window": {"used_percentage": 15.0},
            "quota": {
                "gemini-5h": SAMPLE_QUOTA["gemini-5h"],
                "gemini-argon-wtu-weekly": {"remaining_fraction": 0.84, "reset_in_seconds": 439200},
            },
        }
        plain_legacy = strip_ansi(hud.render_hud(payload_only_legacy, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False))
        rows_legacy = plain_legacy.splitlines()
        self.assertIn(" [Gemini]  5h: 92%", rows_legacy[2])
        self.assertIn("7d: 84% (5d 2h)", rows_legacy[2])
        self.assertNotIn("[Argon]", plain_legacy)

    def test_pool_unmanaged_and_corrupt_db_zero_regression(self):
        import os

        payload = {
            "model": {"display_name": "Gemini 4 Neon (High)", "id": "gemini-4-neon-high"},
            "context_window": {"used_percentage": 20.0},
            "quota": SAMPLE_QUOTA,
        }
        with mock.patch.dict(os.environ, {}, clear=False):
            for k in ("AGY_POOL_THREAD_ID", "AGY_POOL_SEGMENT_ID", "AGY_POOL_ACCOUNT", "AGY_POOL_DB", "AGY_POOL_HOME"):
                os.environ.pop(k, None)
            baseline = hud.render_hud(payload, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False)

        with tempfile.TemporaryDirectory() as tmp:
            bad_db = Path(tmp) / "pool.db"
            bad_db.write_text("not a sqlite database", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"AGY_POOL_THREAD_ID": "T1", "AGY_POOL_SEGMENT_ID": "T1-seg1", "AGY_POOL_ACCOUNT": "main", "AGY_POOL_DB": str(bad_db)},
            ):
                degraded = hud.render_hud(payload, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False)
            self.assertEqual(degraded, baseline)

            os.environ["AGY_POOL_DB"] = str(Path(tmp) / "missing.db")
            missing = hud.render_hud(payload, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False)
            self.assertEqual(missing, baseline)

    def test_pool_badge_and_low_quota_three_states(self):
        import os

        main_hash = hud._pool_email_identity("main@example.test")[1]
        backup_hash = hud._pool_email_identity("backup@example.test")[1]
        now_ts = hud.parse_iso_timestamp("2026-11-26T12:00:00Z")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pool.db"
            # State 1: backup has snapshot 94% updated 2h ago, reset in future
            self._create_pool_db(
                db,
                main_hash=main_hash,
                backup_row={
                    "email_hash": backup_hash,
                    "gemini_5h_remaining": 0.94,
                    "gemini_5h_reset_at": "2026-11-26T15:00:00Z",
                    "gemini_7d_remaining": 0.90,
                    "quota_updated_at": "2026-11-26T10:00:00Z",
                },
            )
            env = {"AGY_POOL_THREAD_ID": "T1", "AGY_POOL_SEGMENT_ID": "T1-seg1", "AGY_POOL_ACCOUNT": "main", "AGY_POOL_DB": str(db)}
            with mock.patch.dict(os.environ, env):
                # 9% > 8%: badge shown, no switch alert
                p9 = {
                    "model": {"display_name": "Gemini 4 Neon"},
                    "email": "main@example.test",
                    "context_window": {"used_percentage": 10.0},
                    "quota": {"gemini-5h": {"remaining_fraction": 0.09, "reset_in_seconds": 3600}},
                }
                plain9 = strip_ansi(hud.render_hud(p9, hud.DEFAULT_CONFIG, now=now_ts, allow_bg_refresh=False))
                self.assertIn("[T1·main·#1]", plain9)
                self.assertNotIn("建议切换", plain9)

                # 8% <= 8%: shows 94%·2h前
                p8 = dict(p9)
                p8["quota"] = {"gemini-5h": {"remaining_fraction": 0.08, "reset_in_seconds": 3600}}
                plain8 = strip_ansi(hud.render_hud(p8, hud.DEFAULT_CONFIG, now=now_ts, allow_bg_refresh=False))
                self.assertIn("⚠ 建议切换 backup（94%·2h前）", plain8)

            # State 2: backup past reset time
            db2 = Path(tmp) / "pool2.db"
            self._create_pool_db(
                db2,
                main_hash=main_hash,
                backup_row={
                    "email_hash": backup_hash,
                    "gemini_5h_remaining": 0.12,
                    "gemini_5h_reset_at": "2026-11-26T11:30:00Z",
                    "quota_updated_at": "2026-11-26T09:00:00Z",
                },
            )
            with mock.patch.dict(os.environ, {**env, "AGY_POOL_DB": str(db2)}):
                plain_rst = strip_ansi(hud.render_hud(p8, hud.DEFAULT_CONFIG, now=now_ts, allow_bg_refresh=False))
                self.assertIn("⚠ 建议切换 backup（已重置）", plain_rst)

            # State 3: backup never observed
            db3 = Path(tmp) / "pool3.db"
            self._create_pool_db(db3, main_hash=main_hash, backup_row={"email_hash": backup_hash})
            with mock.patch.dict(os.environ, {**env, "AGY_POOL_DB": str(db3)}):
                plain_unk = strip_ansi(hud.render_hud(p8, hud.DEFAULT_CONFIG, now=now_ts, allow_bg_refresh=False))
                self.assertIn("⚠ 建议切换 backup（未知余量）", plain_unk)

    def test_pool_account_mismatch_red_alert_overrides_low_quota(self):
        import os

        main_hash = hud._pool_email_identity("main@example.test")[1]
        backup_hash = hud._pool_email_identity("backup@example.test")[1]
        now_ts = hud.parse_iso_timestamp("2026-11-26T12:00:00Z")

        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pool.db"
            self._create_pool_db(
                db,
                main_hash=main_hash,
                backup_row={"email_hash": backup_hash, "gemini_5h_remaining": 0.90, "quota_updated_at": "2026-11-26T11:00:00Z"},
            )
            env = {"AGY_POOL_THREAD_ID": "T1", "AGY_POOL_SEGMENT_ID": "T1-seg1", "AGY_POOL_ACCOUNT": "main", "AGY_POOL_DB": str(db)}
            payload = {
                "model": {"display_name": "Gemini 4 Neon"},
                "email": "backup@example.test",
                "context_window": {"used_percentage": 10.0},
                "quota": {"gemini-5h": {"remaining_fraction": 0.05, "reset_in_seconds": 3600}},
            }
            with mock.patch.dict(os.environ, env):
                rendered = hud.render_hud(payload, hud.DEFAULT_CONFIG, now=now_ts, allow_bg_refresh=False)
            plain = strip_ansi(rendered)
            self.assertIn("⚠ 账号不符：实际 b***@example.test", plain)
            self.assertNotIn("建议切换", plain)
            self.assertIn(hud.RED, rendered)

    def test_pool_live_telemetry_sync_and_cross_account_guard(self):
        import os
        import sqlite3

        main_hash = hud._pool_email_identity("main@example.test")[1]
        hud._POOL_SYNC_LAST.clear()
        with tempfile.TemporaryDirectory() as tmp:
            db = Path(tmp) / "pool.db"
            self._create_pool_db(db, main_hash=main_hash)
            env = {"AGY_POOL_THREAD_ID": "T1", "AGY_POOL_SEGMENT_ID": "T1-seg1", "AGY_POOL_ACCOUNT": "main", "AGY_POOL_DB": str(db)}
            with mock.patch.dict(os.environ, env):
                ctx = hud.load_pool_context(hud.DEFAULT_CONFIG)
                self.assertIsNotNone(ctx)
                payload = {
                    "conversation_id": "conv-live-1",
                    "transcript_path": str(Path(tmp) / "transcript.jsonl"),
                    "email": "main@example.test",
                    "quota": {
                        "gemini-5h": {"remaining_fraction": 0.72, "reset_in_seconds": 3600},
                        "gemini-weekly": {"remaining_fraction": 0.65, "reset_in_seconds": 86400},
                    },
                }
                t0 = 1795700000.0
                self.assertTrue(hud.sync_pool_live_telemetry(payload, hud.parse_stdin_quota_pools(payload, t0), ctx, hud.DEFAULT_CONFIG, now=t0))
                # Debounced within 5s when conversation_id unchanged
                self.assertFalse(hud.sync_pool_live_telemetry(payload, hud.parse_stdin_quota_pools(payload, t0 + 2), ctx, hud.DEFAULT_CONFIG, now=t0 + 2))
                # Mismatched email must NOT overwrite account quota
                bad_payload = {
                    "conversation_id": "conv-live-1",
                    "email": "other@example.test",
                    "quota": {"gemini-5h": {"remaining_fraction": 0.11, "reset_in_seconds": 100}},
                }
                self.assertTrue(hud.sync_pool_live_telemetry(bad_payload, hud.parse_stdin_quota_pools(bad_payload, t0 + 10), ctx, hud.DEFAULT_CONFIG, now=t0 + 10))

            conn = sqlite3.connect(db)
            try:
                seg = conn.execute("SELECT conversation_id, observed_email_hint FROM segments WHERE segment_id='T1-seg1'").fetchone()
                acc = conn.execute("SELECT gemini_5h_remaining, gemini_7d_remaining FROM accounts WHERE account_id='main'").fetchone()
            finally:
                conn.close()
            self.assertEqual(seg[0], "conv-live-1")
            self.assertAlmostEqual(acc[0], 0.72, places=3)
            self.assertAlmostEqual(acc[1], 0.65, places=3)

    def test_apple_vibrancy_light_glass_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "claudespace"
            root.mkdir()
            conv_dir = tmp_path / "conv"
            log_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            log_path.parent.mkdir(parents=True)
            lines = [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-26T12:00:00Z",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/tmp/a.py"}},
                    ],
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-26T12:00:01Z",
                },
                {
                    "step_index": 3,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "RUNNING",
                    "created_at": "2026-11-26T12:00:02Z",
                    "tool_calls": [
                        {"name": "run_command", "args": {"CommandLine": "Verify agy-hud settings"}},
                    ],
                },
            ]
            log_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

            cfg = hud.build_config({"theme": "appleVibrancy"})
            payload = {
                "model": {"display_name": "Gemini 4 Hydrogen (Medium)", "id": "gemini-4-hydrogen"},
                "agent_state": "working",
                "cwd": str(root),
                "context_window": {"used_percentage": 4.0},
                "quota": {
                    "gemini-5h": {"remaining_fraction": 1.0, "reset_in_seconds": 17940},
                    "gemini-weekly": {"remaining_fraction": 0.89, "reset_in_seconds": 579600},
                    "3p-5h": {"remaining_fraction": 0.10, "reset_in_seconds": 10080},
                    "3p-weekly": {"remaining_fraction": 0.05, "reset_in_seconds": 568800},
                },
                "transcript_path": str(log_path),
            }
            with mock.patch.object(hud, "HOME_DIR", tmp_path):
                rendered = hud.render_hud(
                    payload,
                    cfg,
                    now=hud.parse_iso_timestamp("2026-11-26T12:00:10Z"),
                    allow_bg_refresh=False,
                )

            rose = "\x1b[38;2;190;24;93m"
            teal = "\x1b[38;2;15;118;110m"
            blue = "\x1b[38;2;24;86;255m"
            ice_bg = "\x1b[48;2;235;243;255m"
            ok_green = "\x1b[38;2;5;150;105m"
            crimson = "\x1b[38;2;220;38;38m"
            self.assertIn(f"\x1b[1m{rose}Gemini 4 Hydrogen ◔ medium", rendered)
            self.assertNotIn("[Gemini 4 Hydrogen", rendered)
            self.assertIn(f"\x1b[1m{teal}claudespace", rendered)
            self.assertIn(f"{blue}━", rendered)
            self.assertIn("─────────", rendered)
            self.assertIn(ice_bg, rendered)
            self.assertIn(f"{blue}▎", rendered)
            self.assertNotIn(hud.SLATE_ROW_BG, rendered)
            self.assertIn(f"{crimson}10%", rendered)
            self.assertIn("◑", rendered)
            self.assertIn(f"{ok_green}✓", rendered)

            plain_lines = strip_ansi(rendered).splitlines()
            self.assertEqual(plain_lines[2].index("5h:"), plain_lines[3].index("5h:"))
            self.assertEqual(plain_lines[2].index("7d:"), plain_lines[3].index("7d:"))

            # Default config must stay untouched with slate dark row
            default_rendered = hud.render_hud(
                payload,
                hud.DEFAULT_CONFIG,
                now=hud.parse_iso_timestamp("2026-11-26T12:00:10Z"),
                allow_bg_refresh=False,
            )
            self.assertIn(hud.SLATE_ROW_BG, default_rendered)

    def test_mintlify_glass_theme(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            root = tmp_path / "claudespace"
            root.mkdir()
            (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
            conv_dir = tmp_path / "conv"
            log_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
            log_path.parent.mkdir(parents=True)
            lines = [
                {
                    "step_index": 1,
                    "source": "MODEL",
                    "type": "PLANNER_RESPONSE",
                    "status": "DONE",
                    "created_at": "2026-11-26T12:00:00Z",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": "/tmp/a.py"}},
                        {"name": "run_command", "args": {"CommandLine": "ls"}},
                        {"name": "edit_file", "args": {"Path": "/tmp/a.py"}},
                        {"name": "search_web", "args": {"query": "x"}},
                        {"name": "write_file", "args": {"Path": "/tmp/b.py"}},
                    ],
                },
                {
                    "step_index": 2,
                    "source": "MODEL",
                    "type": "GENERIC",
                    "status": "DONE",
                    "created_at": "2026-11-26T12:00:05Z",
                },
            ]
            log_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

            cfg = hud.build_config({"theme": "mintlifyGlass"})
            self.assertTrue(cfg["display"]["showQuotaBars"])
            payload = {
                "model": {"display_name": "Orchestrator (Neon)", "id": "gemini-4-neon"},
                "agent_state": "idle",
                "cwd": str(root),
                "context_window": {"used_percentage": 24.0},
                "quota": {
                    "gemini-5h": {"remaining_fraction": 0.82, "reset_in_seconds": 15540},
                    "gemini-weekly": {"remaining_fraction": 0.83, "reset_in_seconds": 579600},
                    "3p-5h": {"remaining_fraction": 0.10, "reset_in_seconds": 7680},
                    "3p-weekly": {"remaining_fraction": 0.05, "reset_in_seconds": 565200},
                },
                "transcript_path": str(log_path),
            }
            with mock.patch.object(hud, "HOME_DIR", tmp_path):
                rendered = hud.render_hud(
                    payload,
                    cfg,
                    now=hud.parse_iso_timestamp("2026-11-26T12:00:10Z"),
                    allow_bg_refresh=False,
                )

            mint_bg = "\x1b[48;2;230;250;245m"
            mint_fg = "\x1b[38;2;0;153;117m"
            dot_fg = "\x1b[38;2;0;180;138m"
            coral_red = "\x1b[38;2;212;86;86m"
            self.assertIn(f"{dot_fg}● ", rendered)
            self.assertIn(mint_bg, rendered)
            self.assertIn(f"{mint_fg}▎", rendered)
            self.assertIn(f"{coral_red}10%", rendered)
            self.assertNotIn(hud.SLATE_ROW_BG, rendered)

            plain_lines = strip_ansi(rendered).splitlines()
            self.assertEqual(len(plain_lines), 5)
            self.assertIn("上下文 ━━──────── 24%", plain_lines[0])
            self.assertIn("5h ━━━━─ 82% (4h 19m)", plain_lines[1])
            self.assertIn("7d ━━━━─ 83% (6d 17h)", plain_lines[1])
            self.assertIn("5h ━──── 10% (2h 8m)", plain_lines[2])
            self.assertIn("7d ─────  5% (6d 13h)", plain_lines[2])
            self.assertEqual(plain_lines[1].index("5h "), plain_lines[2].index("5h "))
            self.assertEqual(plain_lines[1].index("7d "), plain_lines[2].index("7d "))
            self.assertIn("1 AGENTS", plain_lines[3])
            self.assertNotIn("AGENTS.md", plain_lines[3])
            self.assertEqual(plain_lines[4].count("✓"), 1)
            self.assertTrue(plain_lines[4].endswith("+1"))
            self.assertNotIn("more", plain_lines[4])

    def test_apple_system_blue_theme(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            old_sub_state = hud.SUBAGENT_STATE_FILE
            old_sess_starts = hud.SESSION_STARTS_FILE
            hud.SUBAGENT_STATE_FILE = tmp_path / "subagent-state.json"
            hud.SESSION_STARTS_FILE = tmp_path / "session-starts.json"
            try:
                root = tmp_path / "claudespace"
                root.mkdir()
                (root / "AGENTS.md").write_text("# rules\n", encoding="utf-8")
                conv_dir = tmp_path / "conv"
                log_path = conv_dir / ".system_generated" / "logs" / "transcript.jsonl"
                log_path.parent.mkdir(parents=True)
                lines = [
                    {
                        "step_index": 1,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "DONE",
                        "created_at": "2026-11-26T12:00:00Z",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": "/tmp/a.py"}},
                            {"name": "run_command", "args": {"CommandLine": "ls"}},
                        ],
                    },
                    {
                        "step_index": 2,
                        "source": "MODEL",
                        "type": "GENERIC",
                        "status": "DONE",
                        "created_at": "2026-11-26T12:00:05Z",
                    },
                ]
                log_path.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")

                cfg = hud.build_config({"theme": "appleSystemBlue"})
                self.assertTrue(cfg["display"]["showQuotaBars"])
                payload = {
                    "model": {"display_name": "Orchestrator (Neon)", "id": "gemini-4-neon"},
                    "agent_state": "idle",
                    "cwd": str(root),
                    "context_window": {"used_percentage": 24.0},
                    "quota": {
                        "gemini-5h": {"remaining_fraction": 0.82, "reset_in_seconds": 15540},
                        "gemini-weekly": {"remaining_fraction": 0.83, "reset_in_seconds": 579600},
                        "3p-5h": {"remaining_fraction": 0.10, "reset_in_seconds": 7680},
                        "3p-weekly": {"remaining_fraction": 0.05, "reset_in_seconds": 565200},
                    },
                    "transcript_path": str(log_path),
                }
                with mock.patch.object(hud, "HOME_DIR", tmp_path):
                    rendered = hud.render_hud(
                        payload,
                        cfg,
                        now=hud.parse_iso_timestamp("2026-11-26T12:00:10Z"),
                        allow_bg_refresh=False,
                    )

                idle_green = "\x1b[38;2;22;163;74m"
                graphite = "\x1b[38;2;39;39;42m"
                env_muted = "\x1b[38;2;140;149;159m"
                self.assertIn(f"{idle_green}● ", rendered)
                self.assertIn("\x1b[38;2;29;29;31mOrchestrator (Neon)", rendered)
                self.assertIn("\x1b[38;2;72;72;74mclaudespace", rendered)
                self.assertIn(f"{graphite}━━", rendered)
                self.assertIn("\x1b[48;2;235;244;255m", rendered)
                self.assertIn("\x1b[38;2;0;102;204m▎", rendered)
                self.assertIn("\x1b[38;2;215;0;21m10%", rendered)
                self.assertIn(f"  {env_muted}1 AGENTS", rendered)
                self.assertNotIn("⏱", rendered)
                self.assertNotIn(hud.SLATE_ROW_BG, rendered)

                plain_lines = strip_ansi(rendered).splitlines()
                self.assertEqual(len(plain_lines), 5)
                self.assertEqual(plain_lines[1].index("5h "), plain_lines[2].index("5h "))
                self.assertEqual(plain_lines[1].index("7d "), plain_lines[2].index("7d "))
                self.assertEqual(plain_lines[3].count("✓"), 1)
                self.assertTrue(plain_lines[4].startswith("  1 AGENTS"))

                # 2. Working + Git + Subagents + Todos: amber dot, Col-2 aligned, env stays last
                brain_dir = tmp_path / "conv"
                (brain_dir / "task.md").write_text("- [x] Done step\n- [/] Build R2 UI\n- [ ] Verify\n", encoding="utf-8")
                work_lines = list(lines)
                work_lines.append(
                    {
                        "step_index": 3,
                        "source": "MODEL",
                        "type": "PLANNER_RESPONSE",
                        "status": "RUNNING",
                        "created_at": "2026-11-26T12:00:08Z",
                        "tool_calls": [
                            {"name": "run_command", "args": {"CommandLine": "python3 -m unittest test_hud.py"}},
                        ],
                    }
                )
                log_path.write_text("\n".join(json.dumps(x) for x in work_lines) + "\n", encoding="utf-8")
                work_payload = {
                    **payload,
                    "model": {"display_name": "Gemini 4 Neon (High)", "id": "gemini-4-neon"},
                    "agent_state": "working",
                    "vcs": {"branch": "main", "dirty": True},
                    "subagents": [
                        {
                            "conversation_id": "sub-1",
                            "role": "Codebase Researcher",
                            "model": "flash",
                            "description": "Inspect quota alignment",
                            "status": "running",
                            "started_at": "2026-11-26T12:00:02Z",
                        },
                        {
                            "conversation_id": "sub-2",
                            "role": "Skill Installer",
                            "model": "hydrogen",
                            "description": "Validate skills",
                            "status": "completed",
                            "started_at": "2026-11-26T11:59:40Z",
                            "ended_at": "2026-11-26T12:00:02Z",
                        },
                        {
                            "conversation_id": "sub-3",
                            "role": "Test Runner",
                            "model": "flash",
                            "description": "unittest failed",
                            "status": "error",
                            "started_at": "2026-11-26T11:59:55Z",
                            "ended_at": "2026-11-26T12:00:04Z",
                        },
                    ],
                }
                porcelain = " M a.py\n M b.py\nA  c.py\n?? d.py\n"
                with (
                    mock.patch.object(hud, "HOME_DIR", tmp_path),
                    mock.patch.object(hud.subprocess, "check_output", return_value=porcelain),
                ):
                    work_rendered = hud.render_hud(
                        work_payload,
                        cfg,
                        now=hud.parse_iso_timestamp("2026-11-26T12:00:16Z"),
                        allow_bg_refresh=False,
                    )

                amber = "\x1b[38;2;217;119;6m"
                red = "\x1b[38;2;215;0;21m"
                self.assertIn(f"{amber}● ", work_rendered)
                self.assertIn(f"{idle_green}+1", work_rendered)
                self.assertIn("\x1b[1m\x1b[38;2;29;29;31mShell:", work_rendered)
                self.assertIn("\x1b[1m\x1b[38;2;29;29;31mCodebase Researcher", work_rendered)
                self.assertIn(f"\x1b[1m{red}Test Runner", work_rendered)
                work_plain = strip_ansi(work_rendered).splitlines()
                self.assertIn("git:(main* !2 +1 ?1)", work_plain[0])
                self.assertTrue(work_plain[3].startswith("◑ Shell:"))
                self.assertTrue(work_plain[4].startswith("◐ Codebase Researcher"))
                self.assertTrue(work_plain[5].startswith("✓ Skill Installer"))
                self.assertTrue(work_plain[6].startswith("✗ Test Runner"))
                self.assertTrue(work_plain[7].startswith("▸ Build R2 UI (1/3)"))
                self.assertTrue(work_plain[-1].startswith("  1 AGENTS"))

                # 3. Argon model merged into Gemini (2 pools only) + dedicated agy-pool alert + error state
                main_hash = hud._pool_email_identity("main@example.test")[1]
                backup_hash = hud._pool_email_identity("backup@example.test")[1]
                now_ts = hud.parse_iso_timestamp("2026-11-26T12:00:00Z")
                db = tmp_path / "pool.db"
                self._create_pool_db(
                    db,
                    main_hash=main_hash,
                    backup_row={
                        "email_hash": backup_hash,
                        "gemini_5h_remaining": 0.94,
                        "gemini_5h_reset_at": "2026-11-26T15:00:00Z",
                        "gemini_7d_remaining": 0.90,
                        "quota_updated_at": "2026-11-26T11:58:00Z",
                    },
                )
                pool_env = {
                    "AGY_POOL_THREAD_ID": "T1",
                    "AGY_POOL_SEGMENT_ID": "T1-seg1",
                    "AGY_POOL_ACCOUNT": "main",
                    "AGY_POOL_DB": str(db),
                }
                argon_alert_payload = {
                    "model": {"display_name": "Gemini 4 Argon (High)", "id": "gemini-4-argon-high"},
                    "agent_state": "error",
                    "email": "main@example.test",
                    "cwd": str(root),
                    "context_window": {
                        "used_percentage": 89.0,
                        "total_input_tokens": 178000,
                        "total_output_tokens": 14000,
                    },
                    "quota": {
                        "gemini-5h": {"remaining_fraction": 0.04, "reset_in_seconds": 4320},
                        "gemini-weekly": {"remaining_fraction": 0.62, "reset_in_seconds": 496800},
                        "3p-5h": {"remaining_fraction": 0.58, "reset_in_seconds": 12000},
                        "3p-weekly": {"remaining_fraction": 0.79, "reset_in_seconds": 554400},
                    },
                }
                with mock.patch.dict(os.environ, pool_env), mock.patch.object(hud, "HOME_DIR", tmp_path):
                    crit_rendered = hud.render_hud(argon_alert_payload, cfg, now=now_ts, allow_bg_refresh=False)
                self.assertIn(f"{red}● ", crit_rendered)
                self.assertIn("\x1b[38;2;110;119;129m[T1·main·#1]", crit_rendered)
                # Inactive healthy 3P mini-bar stays muted #6E7781 instead of active blue
                self.assertIn("\x1b[38;2;110;119;129m━━━", crit_rendered)
                crit_plain = strip_ansi(crit_rendered).splitlines()
                self.assertNotIn("Argon", "\n".join(crit_plain[1:]))
                self.assertTrue(crit_plain[1].startswith("▎ Gemini"))
                self.assertTrue(crit_plain[2].startswith("  3P"))
                self.assertTrue(crit_plain[3].startswith("⚠ 建议切换 backup（94%·2m前）"))
                self.assertNotIn("⚠ 建议切换", crit_plain[0])
                self.assertTrue(crit_plain[-1].startswith("  1 AGENTS"))
            finally:
                hud.SUBAGENT_STATE_FILE = old_sub_state
                hud.SESSION_STARTS_FILE = old_sess_starts

    def test_quartz_teal_theme(self):
        cfg = hud.build_config({"theme": "quartzTeal", "maxWidth": 120})
        payload = {
            "model": {"display_name": "Gemini 4 Argon (High)", "id": "gemini-4-argon-high"},
            "context_window": {"used_percentage": 89},
            "quota": {
                "argon-5h": {"remaining_fraction": .21, "reset_in_seconds": 5520},
                "argon-weekly": {"remaining_fraction": .68, "reset_in_seconds": 568800},
                "3p-5h": {"remaining_fraction": 1, "reset_in_seconds": 0},
                "3p-weekly": {"remaining_fraction": .05, "reset_in_seconds": 554400},
            },
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload["cwd"] = tmp
            (root / "AGENTS.md").write_text("# Rules", encoding="utf-8")
            (root / ".agents").mkdir()
            (root / ".agents/mcp_config.json").write_text(
                '{"mcpServers":{"a":{},"b":{},"c":{}}}', encoding="utf-8",
            )
            with mock.patch.object(hud, "HOME_DIR", root):
                rendered = hud.render_hud(payload, cfg, now=1000, allow_bg_refresh=False)
        lines = strip_ansi(rendered).splitlines()
        self.assertEqual(len(lines), 4, lines)
        head, active, inactive, footer = lines
        teal, track, muted, env, warn, crit = (
            "\x1b[38;2;11;99;92m", "\x1b[38;2;110;125;144m", "\x1b[38;2;82;96;113m",
            "\x1b[38;2;91;104;120m", "\x1b[38;2;154;82;4m", "\x1b[38;2;185;28;28m",
        )
        self.assertNotIn("\x1b[48;", rendered)
        self.assertTrue(active.startswith("┃ Gemini   5h ━┄┄┄┄  21%△"))
        self.assertIn("68%  (6d 14h)", active)
        self.assertTrue(inactive.startswith("┆ 3P"))
        self.assertIn("100%  (now)", inactive)
        self.assertEqual(active.index("5h "), inactive.index("5h "))
        self.assertEqual(active.index("7d "), inactive.index("7d "))
        self.assertEqual(hud.visual_width("△"), 1)
        self.assertIn(f"{teal}┃{hud.RESET} \x1b[1m{teal}Gemini", rendered)
        self.assertIn(f"{track}┆{hud.RESET} {muted}3P", rendered)
        self.assertIn(f"{track}┄", rendered)
        self.assertIn(f"\x1b[1m{warn} 21%△", rendered)
        self.assertIn(f"\x1b[1m{crit}  5%!", rendered)
        self.assertIn(f"\x1b[1m{crit}89%!", rendered)
        self.assertIn(" │ 上下文 ", head)
        self.assertNotIn("·", head)
        self.assertEqual(footer, "  1 AGENTS / 3 MCPs")
        self.assertIn(f"  {env}1 AGENTS{hud.RESET}{env} / ", rendered)

    def test_quartz_teal_tool_groups_and_colored_wrap(self):
        cfg = hud.build_config({"theme": "quartzTeal", "maxWidth": 60})
        transcript = {"tools": [
            {"name": "run_command", "status": "running", "target": "x" * 45},
            {"name": "read_file", "status": "completed"},
            {"name": "edit_file", "status": "completed"},
        ]}
        tools = hud.render_tools_line(transcript, cfg)
        self.assertIn(" \x1b[38;2;110;125;144m│\x1b[0m ", tools)
        self.assertEqual(strip_ansi(tools).count("✓"), 1)
        left, right = hud.wrap_line(tools, 60)
        self.assertTrue(left.endswith(hud.RESET))
        self.assertTrue(right.startswith("\x1b[38;2;15;107;67m✓"))
        footer = hud.render_environment_line({
            "agentsMdCount": 123456789, "rulesCount": 123456789,
            "mcpCount": 123456789, "skillsCount": 123456789, "hooksCount": 123456789,
        }, cfg)
        for line, delim in ((tools, " │ "), (footer, " / ")):
            wrapped = hud.wrap_line(line, hud.get_terminal_width(cfg))
            self.assertGreater(len(wrapped), 1)
            self.assertTrue(all(hud.visual_width(row) <= 60 for row in wrapped))
            self.assertEqual(delim.join(map(strip_ansi, wrapped)), strip_ansi(line))

    def test_legacy_themes_do_not_wrap_inside_slash_separated_models(self):
        title = f"{'a' * 29} / {'b' * 29}"
        for theme in ("default", "appleVibrancy", "mintlifyGlass", "appleSystemBlue"):
            with self.subTest(theme=theme):
                cfg = hud.build_config({"theme": theme, "maxWidth": 60})
                payload = {"model": {"display_name": title}, "quota": SAMPLE_QUOTA}
                rendered = hud.render_hud(payload, cfg, now=1000, allow_bg_refresh=False)
                self.assertIn(title, strip_ansi(rendered))

    def test_quartz_teal_alert_and_failed_agent(self):
        import os

        now = hud.parse_iso_timestamp("2026-11-26T12:00:00Z")
        cfg = hud.build_config({"theme": "quartzTeal", "maxWidth": 120})
        payload = {
            "model": {"display_name": "Gemini 4 Argon (High)"},
            "agent_state": "error",
            "email": "main@example.test",
            "context_window": {"used_percentage": 89, "total_input_tokens": 178000,
                               "total_output_tokens": 14000},
            "sandbox": {"enabled": True},
            "quota": {"gemini-5h": {"remaining_fraction": .04, "reset_in_seconds": 4320}},
            "subagents": [{"conversation_id": "failed", "role": "Test Runner", "model": "flash",
                           "status": "error", "description": "unittest failed (1 error)",
                           "started_at": "2026-11-26T11:59:51Z", "ended_at": "2026-11-26T12:00:00Z"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root / "pool.db"
            self._create_pool_db(
                db,
                main_hash=hud._pool_email_identity("main@example.test")[1],
                backup_row={
                    "email_hash": hud._pool_email_identity("backup@example.test")[1],
                    "gemini_5h_remaining": .94,
                    "gemini_5h_reset_at": "2026-11-26T15:00:00Z",
                    "gemini_7d_remaining": .90,
                    "quota_updated_at": "2026-11-26T11:58:00Z",
                },
            )
            env = {"AGY_POOL_THREAD_ID": "T1", "AGY_POOL_SEGMENT_ID": "T1-seg1",
                   "AGY_POOL_ACCOUNT": "main", "AGY_POOL_DB": str(db)}
            with (mock.patch.dict(os.environ, env), mock.patch.object(hud, "HOME_DIR", root),
                  mock.patch.object(hud, "SUBAGENT_STATE_FILE", root / "subagents.json")):
                rendered = hud.render_hud(payload, cfg, now=now, allow_bg_refresh=False)
        head, quota, alert, agent = strip_ansi(rendered).splitlines()
        self.assertIn("[T1·main·#1]", head)
        self.assertIn("[sandbox] │ 上下文", head)
        self.assertIn("89%! (输入: 178k, 输出: 14k)", head)
        self.assertTrue(quota.startswith("┃ Gemini"))
        self.assertEqual(alert, "⚠ 建议切换 backup（94%·2m前）")
        self.assertTrue(agent.startswith("✗ Test Runner"))
        self.assertIn("\x1b[1m\x1b[38;2;185;28;28mTest Runner", rendered)

    def test_quartz_teal_aligns_weekly_quota_even_without_an_inactive_5h_bucket(self):
        data = {"model": {"id": "gemini-4-neon"}}
        quota = {"gemini": {"5h": {"remaining_percentage": 82}, "7d": {"remaining_percentage": 88}},
                 "3p": {"7d": {"remaining_percentage": 5}}}
        cfg = hud.build_config({"theme": "quartzTeal"})
        _, rows = hud.render_usage_blocks(data, quota, {}, cfg)
        active, inactive = map(strip_ansi, rows)
        self.assertNotIn("5h", inactive)
        self.assertEqual(active.index("7d "), inactive.index("7d "))

    def test_quartz_teal_fractional_quota_thresholds_match_the_bar(self):
        quota = {"gemini": {"5h": {"remaining_percentage": 25},
                            "7d": {"remaining_percentage": 10.1}}}
        data = {"model": {"id": "gemini-4-neon"}}
        for mode, expected in (("remaining", ("25%△", "10%△")),
                               ("used", ("75%△", "90%△"))):
            with self.subTest(mode=mode):
                cfg = hud.build_config({"theme": "quartzTeal", "display": {"usageValue": mode}})
                _, (row,) = hud.render_usage_blocks(data, quota, {}, cfg)
                self.assertFalse(strip_ansi(row).endswith(" "))
                self.assertEqual(row.count("\x1b[38;2;154;82;4m"), 4)
                self.assertNotIn("\x1b[38;2;185;28;28m", row)
                for value in expected:
                    self.assertIn(value, strip_ansi(row))

    def test_frosted_pills_renders_g1_three_line_layout(self):
        cfg = hud.build_config({"theme": "frostedPills", "maxWidth": 120})
        payload = {
            "model": {"display_name": "Gemini 4 Hydrogen (Medium)"},
            "plan_tier": "Google AI Pro",
            "cwd": "/tmp/claudespace",
            "agent_state": "idle",
            "context_window": {"used_percentage": 8, "context_window_size": 200000, "used_tokens": 16000},
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.88, "reset_in_seconds": 7680},
                "gemini-weekly": {"remaining_fraction": 0.57, "reset_in_seconds": 504000},
                "3p-5h": {"remaining_fraction": 1.0, "reset_in_seconds": 0},
                "3p-weekly": {"remaining_fraction": 0.05, "reset_in_seconds": 489600},
            },
        }
        counts = {"agentsMdCount": 1, "rulesCount": 0, "mcpCount": 3, "skillsCount": 24, "hooksCount": 1}
        git_info = {"branch": "main", "dirty": False, "ahead": 0, "behind": 0}
        with (
            mock.patch.object(hud, "count_agy_configs", return_value=counts),
            mock.patch.object(hud, "get_git_status", return_value=git_info),
            mock.patch.object(hud, "resolve_transcript_path", return_value=None),
        ):
            rendered = hud.render_hud(payload, cfg, now=1000, allow_bg_refresh=False)

        lines = rendered.splitlines()
        self.assertEqual(len(lines), 3)
        head, pools, footer = map(strip_ansi, lines)
        self.assertNotIn("AI Pro", head + pools + footer)
        self.assertIn("\uE0B6● Gemini 4 Hydrogen · medium\uE0B4", head)
        self.assertIn("⎇ main", head)
        self.assertIn("CTX ▰▱▱▱▱▱▱ 8%", head)
        self.assertIn("\x1b[48;2;235;243;250m", lines[0])
        self.assertIn("\uE0B6▸ Gemini", pools)
        self.assertIn("5h ━━━━━┄ 88%", pools)
        self.assertIn("7d ━━━┄┄┄ 57%", pools)
        self.assertIn("○ 3P  5h 100% · 7d 5%!", pools)
        self.assertIn("\x1b[48;2;232;245;243m", lines[1])
        self.assertIn("\x1b[1m\x1b[38;2;220;38;38m5%!", lines[1])
        self.assertIn("● Idle", footer)
        self.assertIn("1 AGENTS · 3 MCPs · 24 Skills · 1 钩子", footer)
        self.assertIn("\x1b[1m\x1b[38;2;15;23;42m24\x1b[0m", lines[2])

    def test_frosted_pills_rect_fallback_without_nerd_font(self):
        cfg = hud.build_config({"theme": "frostedPills", "style": {"pillRounded": False}, "maxWidth": 120})
        payload = {
            "model": {"display_name": "Gemini 4 Neon (High)"},
            "quota": SAMPLE_QUOTA,
        }
        rendered = hud.render_hud(payload, cfg, now=1000, allow_bg_refresh=False)
        self.assertNotIn("\uE0B6", rendered)
        self.assertNotIn("\uE0B4", rendered)
        self.assertIn("\x1b[48;2;235;243;250m", rendered)
        self.assertIn("\x1b[48;2;232;245;243m", rendered)

    def test_frosted_pills_danger_switches_to_rose_bg_and_handoff_hint(self):
        cfg = hud.build_config({"theme": "frostedPills", "maxWidth": 120})
        payload = {
            "model": {"display_name": "Gemini 4 Neon (High)"},
            "quota": {
                "gemini-5h": {"remaining_fraction": 0.04, "reset_in_seconds": 2280},
                "gemini-weekly": {"remaining_fraction": 0.21, "reset_in_seconds": 378000},
                "3p-5h": {"remaining_fraction": 1.0, "reset_in_seconds": 0},
            },
        }
        rendered = hud.render_hud(payload, cfg, now=1000, allow_bg_refresh=False)
        self.assertIn("\x1b[48;2;252;238;239m", rendered)
        self.assertNotIn("\x1b[48;2;232;245;243m", rendered)
        self.assertIn("4%! (38m)", strip_ansi(rendered))
        self.assertIn("↻ 建议换号", strip_ansi(rendered))

    def test_frosted_pills_narrow_width_splits_pools_without_breaking_pill(self):
        cfg = hud.build_config({"theme": "frostedPills", "maxWidth": 60})
        data = {"model": {"display_name": "Gemini 4 Hydrogen (Medium)"}}
        pools = {
            "gemini": {
                "5h": {"remaining_percentage": 88, "refreshes_in": "2h 8m"},
                "7d": {"remaining_percentage": 57, "refreshes_in": "5d 20h"},
            },
            "3p": {
                "5h": {"remaining_percentage": 100, "refreshes_in": "now"},
                "7d": {"remaining_percentage": 5, "refreshes_in": "5d 16h"},
            },
        }
        _, rows = hud.render_usage_blocks(data, pools, {}, cfg, max_width=60)
        self.assertEqual(len(rows), 2)
        self.assertTrue(strip_ansi(rows[0]).startswith("\uE0B6▸ Gemini"))
        self.assertTrue(strip_ansi(rows[0]).endswith("\uE0B4"))
        self.assertTrue(strip_ansi(rows[1]).startswith("○ 3P"))

    def test_frosted_pills_without_quota_hides_plan_tier_and_does_not_crash(self):
        cfg = hud.build_config({"theme": "frostedPills", "maxWidth": 120})
        payload = {
            "model": {"display_name": "Gemini 4 Neon (High)"},
            "plan_tier": "Google AI Pro",
            "cwd": "/tmp/claudespace",
            "agent_state": "idle",
        }
        rendered = hud.render_hud(payload, cfg, now=1000, allow_bg_refresh=False)
        plain = strip_ansi(rendered)
        self.assertNotIn("AI Pro", plain)
        self.assertIn("Gemini 4 Neon", plain)


if __name__ == "__main__":
    unittest.main()
