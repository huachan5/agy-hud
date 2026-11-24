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
            self.assertEqual(len(rows), 4, f"Expected 4 rows, got:\n{plain}")
            self.assertIn("[Gemini 4 Neon ◑ high]", rows[0])
            self.assertIn("cspace git:(feat/hud*)", rows[0])
            self.assertIn("上下文", rows[1])
            self.assertIn("7%", rows[1])
            self.assertIn("Gemini 5h: 92% (4h 12m) | 7d: 88% (5d 3h)", rows[1])
            self.assertIn("3P 5h: 75% (2h 40m) | 7d: 81% (6d 1h)", rows[1])
            # Active Gemini pool comes before 3P pool
            self.assertLess(rows[1].index("Gemini"), rows[1].index("3P"))
            self.assertNotIn("Codex", plain)
            self.assertIn("1 AGENTS.md", rows[2])
            self.assertIn("1 MCPs", rows[2])
            self.assertIn("1 Skills", rows[2])
            self.assertIn("✓ Read ×2", rows[3])
            self.assertIn("+1 more", rows[3])

    def test_3p_model_orders_3p_first_and_active_only_mode(self):
        payload = {
            "model": {"display_name": "Claude Sonnet 4.6", "id": "claude-sonnet-4-6"},
            "context_window": {"used_percentage": 25.0},
            "quota": SAMPLE_QUOTA,
        }
        both_plain = strip_ansi(hud.render_hud(payload, hud.DEFAULT_CONFIG, now=1000.0, allow_bg_refresh=False))
        self.assertLess(both_plain.index("3P"), both_plain.index("Gemini"))

        cfg_active = hud._deep_merge(hud.DEFAULT_CONFIG, {"display": {"quotaPools": "active"}})
        active_plain = strip_ansi(hud.render_hud(payload, cfg_active, now=1000.0, allow_bg_refresh=False))
        self.assertIn("3P 5h: 75%", active_plain)
        self.assertNotIn("Gemini 5h", active_plain)

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
        self.assertIn("Gemini 5h: 30% (1h)", plain)

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
        self.assertIn("Gemini 5h: 92%", plain)
        self.assertIn("3P 5h: 75%", plain)
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


if __name__ == "__main__":
    unittest.main()
