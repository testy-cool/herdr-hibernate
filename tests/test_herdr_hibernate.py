import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
import unittest
from unittest import mock


SCRIPT = os.path.join(os.path.dirname(os.path.dirname(__file__)), "herdr-hibernate")
LOADER = importlib.machinery.SourceFileLoader("herdr_hibernate_module", SCRIPT)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
hibernate = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(hibernate)


class HibernateTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        root = self.tempdir.name
        self.paths = mock.patch.multiple(
            hibernate,
            CONFIG_DIR=root,
            STATE_FILE=os.path.join(root, "state.json"),
            LOG_FILE=os.path.join(root, "hibernate.log"),
            PANES_DIR=os.path.join(root, "panes"),
            PID_FILE=os.path.join(root, "watch.pid"),
            WATCH_LOCK_FILE=os.path.join(root, "watch.lock"),
            OPERATION_LOCK_FILE=os.path.join(root, "operation.lock"),
        )
        self.paths.start()

    def tearDown(self):
        self.paths.stop()
        self.tempdir.cleanup()

    def test_file_lock_allows_only_one_owner(self):
        with hibernate.file_lock(hibernate.WATCH_LOCK_FILE, blocking=False) as first:
            self.assertTrue(first)
            with hibernate.file_lock(
                    hibernate.WATCH_LOCK_FILE, blocking=False) as second:
                self.assertFalse(second)

    def test_collect_panes_merges_names_from_agent_list(self):
        def fake_herdr(*args):
            if args == ("workspace", "list"):
                return {"workspaces": [{"workspace_id": "work-a"}]}
            if args[:2] == ("tab", "list"):
                return {"tabs": []}
            if args[:2] == ("pane", "list"):
                return {"panes": [{
                    "pane_id": "p1", "agent": "codex",
                    "workspace_id": "work-a",
                }]}
            if args == ("agent", "list"):
                return {"agents": [{
                    "pane_id": "p1", "name": "project-worker",
                }]}
            raise AssertionError("unexpected Herdr call: %r" % (args,))

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            panes, _labels = hibernate.collect_panes()

        self.assertEqual(panes[0]["name"], "project-worker")

    def test_codex_resume_replays_yolo_aliases(self):
        sid = "90141d62-7130-4dd0-8083-211884e8e999"
        for flag in (
                "--yolo",
                "--dangerously-bypass-approvals-and-sandbox"):
            with self.subTest(flag=flag):
                proc = {
                    "argv": ["node", "/tmp/bin/codex", "-c",
                             "model_reasoning_effort=xhigh", flag],
                }

                resume = hibernate.build_resume(
                    "codex", sid, proc, "/tmp/project")

                self.assertEqual(resume, [
                    "codex", "resume", sid,
                    "-c", "model_reasoning_effort=xhigh", flag,
                ])

    def test_workspace_records_follow_live_workspace_ids(self):
        select = getattr(hibernate, "workspace_records", None)
        self.assertIsNotNone(select, "workspace record selection is missing")
        if select is None:
            return
        panes = [
            {"pane_id": "opaque-a", "workspace_id": "work-a"},
            {"pane_id": "opaque-b", "workspace_id": "work-b"},
        ]
        state = {
            "opaque-a": {"uuid": "a"},
            "opaque-b": {"uuid": "b"},
            "renumbered": {"uuid": "c", "workspace_id": "work-a"},
        }

        selected = select("work-a", panes, state)

        self.assertEqual([pane for pane, _ in selected], [
            "opaque-a", "renumbered",
        ])

    def test_workspace_preflight_blocks_the_entire_batch(self):
        preflight = getattr(hibernate, "workspace_preflight", None)
        self.assertIsNotNone(preflight, "workspace preflight is missing")
        if preflight is None:
            return
        panes = [
            {
                "pane_id": "p-idle", "workspace_id": "work-a",
                "agent": "codex", "agent_status": "idle",
            },
            {
                "pane_id": "p-working", "workspace_id": "work-a",
                "agent": "codex", "agent_status": "working",
            },
        ]
        cfg = dict(hibernate.DEFAULTS)

        def classify(pane, *_args):
            if pane["pane_id"] == "p-idle":
                return "skip", "idle only 1m (< 30m threshold)"
            return "exempt", "status working"

        with mock.patch.object(hibernate, "classify", side_effect=classify), \
                mock.patch.object(hibernate, "session_uuid",
                                  return_value=(
                                      "90141d62-7130-4dd0-8083-211884e8e999")), \
                mock.patch.object(hibernate, "find_agent_proc",
                                  return_value=(1234, {"pid": 1234})), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value={"shell_pid": 99}):
            targets, blockers = preflight(
                "work-a", panes, {}, cfg, {})

        self.assertEqual([p["pane_id"] for p, _ in targets], ["p-idle"])
        self.assertEqual(blockers, ["p-working: status working"])

    def test_workspace_wake_sends_enter_to_every_stub_with_stagger(self):
        wake = getattr(hibernate, "cmd_wake_workspace", None)
        self.assertIsNotNone(wake, "workspace wake command is missing")
        if wake is None:
            return
        panes = [
            {"pane_id": "p1", "workspace_id": "work-a"},
            {"pane_id": "p2", "workspace_id": "work-a"},
        ]
        state = {
            "p1": {"uuid": "a", "workspace_id": "work-a"},
            "p2": {"uuid": "b", "workspace_id": "work-a"},
        }
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            return {}

        cfg = dict(hibernate.DEFAULTS)
        cfg["WORKSPACE_WAKE_STAGGER_SECONDS"] = "0.25"
        with mock.patch.object(hibernate, "load_config", return_value=cfg), \
                mock.patch.object(hibernate, "load_state", return_value=state), \
                mock.patch.object(hibernate, "collect_panes",
                                  return_value=(panes, {})), \
                mock.patch.object(hibernate, "restore_records"), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value={
                                      "foreground_processes": [{
                                          "cmdline": hibernate.PANES_DIR,
                                      }],
                                  }), \
                mock.patch.object(hibernate, "stub_alive", return_value=True), \
                mock.patch.object(hibernate, "herdr", side_effect=fake_herdr), \
                mock.patch.object(hibernate.time, "sleep") as sleep:
            wake("work-a")

        self.assertEqual([
            call for call in calls if call[:2] == ("pane", "send-keys")
        ], [
            ("pane", "send-keys", "p1", "enter"),
            ("pane", "send-keys", "p2", "enter"),
        ])
        sleep.assert_called_once_with(0.25)

    def test_workspace_label_restores_after_last_pane_wakes(self):
        restore = getattr(hibernate, "restore_owned_workspace_label", None)
        self.assertIsNotNone(restore, "workspace marker restore is missing")
        if restore is None:
            return
        rec = {
            "workspace_id": "work-a",
            "workspace_label": "My project",
            "workspace_marker_added": True,
            "marked_workspace_label": "💤 My project",
        }
        state = {"p1": rec}
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            if args[:2] == ("workspace", "get"):
                return {"workspace": {"label": "💤 My project"}}
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            ok = restore("p1", rec, state)

        self.assertTrue(ok)
        self.assertIn(
            ("workspace", "rename", "work-a", "My project"), calls)

    def test_agent_name_restores_after_wake(self):
        restore = getattr(hibernate, "restore_owned_agent_name", None)
        self.assertIsNotNone(restore, "agent name restore is missing")
        if restore is None:
            return
        rec = {"agent_name": "project-worker"}
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            ok = restore("p1", {"pane_id": "p1"}, rec)

        self.assertTrue(ok)
        self.assertIn(
            ("agent", "rename", "p1", "project-worker"), calls)

    def test_project_wake_releases_its_workspace_marker(self):
        release = getattr(hibernate, "release_workspace_marker", None)
        self.assertIsNotNone(release, "workspace marker release is missing")
        if release is None:
            return
        state = {
            "p1": {
                "workspace_id": "work-a", "workspace_label": "My project",
                "workspace_marker_added": True,
                "marked_workspace_label": "💤 My project",
            },
            "p2": {
                "workspace_id": "work-a", "workspace_label": "My project",
                "workspace_marker_added": True,
                "marked_workspace_label": "💤 My project",
            },
        }
        records = list(state.items())
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            if args[:2] == ("workspace", "get"):
                return {"workspace": {"label": "💤 My project"}}
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr), \
                mock.patch.object(hibernate, "save_state") as save:
            release("work-a", records, state)

        self.assertIn(
            ("workspace", "rename", "work-a", "My project"), calls)
        self.assertTrue(all(
            rec["workspace_marker_added"] is False for rec in state.values()))
        save.assert_called_once_with(state)

    def test_forgetting_last_parked_pane_restores_workspace_label(self):
        rec = {
            "workspace_id": "work-a", "workspace_label": "My project",
            "workspace_marker_added": True,
            "marked_workspace_label": "💤 My project",
            "label": "Agent pane",
        }
        state = {"p1": rec}
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            if args[:2] == ("workspace", "get"):
                return {"workspace": {"label": "💤 My project"}}
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr), \
                mock.patch.object(hibernate, "save_state"), \
                mock.patch.object(hibernate, "remove_stub_file"), \
                mock.patch.object(hibernate, "log"):
            hibernate.forget_record("p1", state, "tab closed")

        self.assertIn(
            ("workspace", "rename", "work-a", "My project"), calls)
        self.assertEqual(state, {})

    def test_workspace_hibernate_refuses_before_touching_any_pane(self):
        hibernate_workspace = getattr(
            hibernate, "cmd_hibernate_workspace", None)
        self.assertIsNotNone(
            hibernate_workspace, "workspace hibernate command is missing")
        if hibernate_workspace is None:
            return
        pane = {"pane_id": "p-idle", "workspace_id": "work-a"}
        cfg = dict(hibernate.DEFAULTS)
        cfg["DRY_RUN"] = "0"

        with mock.patch.object(hibernate, "load_config", return_value=cfg), \
                mock.patch.object(hibernate, "load_state", return_value={}), \
                mock.patch.object(hibernate, "collect_panes",
                                  return_value=([pane], {})), \
                mock.patch.object(hibernate, "restore_records"), \
                mock.patch.object(hibernate, "workspace_preflight",
                                  return_value=([(pane, "1m")], [
                                      "p-working: status working",
                                  ])), \
                mock.patch.object(hibernate, "hibernate_pane") as park:
            with self.assertRaisesRegex(
                    SystemExit, "p-working: status working"):
                hibernate_workspace("work-a")

        park.assert_not_called()

    def test_workspace_hibernate_parks_every_preflighted_pane(self):
        hibernate_workspace = getattr(
            hibernate, "cmd_hibernate_workspace", None)
        self.assertIsNotNone(
            hibernate_workspace, "workspace hibernate command is missing")
        if hibernate_workspace is None:
            return
        panes = [
            {"pane_id": "p1", "workspace_id": "work-a"},
            {"pane_id": "p2", "workspace_id": "work-a"},
        ]
        state = {}
        cfg = dict(hibernate.DEFAULTS)
        cfg["DRY_RUN"] = "0"

        def park(pane, _labels, _cfg, live_state, *_args, **_kwargs):
            live_state[pane["pane_id"]] = {
                "uuid": pane["pane_id"], "workspace_id": "work-a",
            }
            return True

        with mock.patch.object(hibernate, "load_config", return_value=cfg), \
                mock.patch.object(hibernate, "load_state", return_value=state), \
                mock.patch.object(hibernate, "collect_panes",
                                  return_value=(panes, {})), \
                mock.patch.object(hibernate, "restore_records"), \
                mock.patch.object(hibernate, "workspace_preflight",
                                  return_value=([
                                      (panes[0], "1m"),
                                      (panes[1], "2m"),
                                  ], [])), \
                mock.patch.object(hibernate, "hibernate_pane",
                                  side_effect=park) as park_pane, \
                mock.patch.object(
                    hibernate, "mark_workspace_hibernated") as mark:
            hibernate_workspace("work-a")

        self.assertEqual(park_pane.call_count, 2)
        mark.assert_called_once_with("work-a", ["p1", "p2"], state)

    def test_workspace_toggle_wakes_a_fully_parked_workspace(self):
        toggle = getattr(hibernate, "cmd_workspace_toggle", None)
        self.assertIsNotNone(toggle, "workspace toggle command is missing")
        if toggle is None:
            return
        panes = [{"pane_id": "p1", "workspace_id": "work-a"}]
        state = {"p1": {"uuid": "a", "workspace_id": "work-a"}}

        def fake_herdr(*args):
            if args == ("pane", "current"):
                return {"pane": {
                    "pane_id": "p1", "workspace_id": "work-a",
                }}
            raise AssertionError("unexpected Herdr call: %r" % (args,))

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr), \
                mock.patch.object(hibernate, "load_state", return_value=state), \
                mock.patch.object(hibernate, "collect_panes",
                                  return_value=(panes, {})), \
                mock.patch.object(hibernate, "cmd_wake_workspace") as wake, \
                mock.patch.object(hibernate, "cmd_hibernate_workspace") as park:
            toggle(notify=True)

        wake.assert_called_once_with("work-a", notify=True)
        park.assert_not_called()

    def test_restore_rekey_updates_workspace_ownership(self):
        rec = {
            "uuid": "90141d62-7130-4dd0-8083-211884e8e999",
            "agent": "claude", "tab_id": "tab-stable",
            "workspace_id": "workspace-old", "label": "My project",
            "cwd": "/tmp/project", "marker_added": False,
        }
        state = {"pane-old": rec}
        pane = {
            "pane_id": "pane-new", "tab_id": "tab-stable",
            "workspace_id": "workspace-new", "cwd": "/tmp/project",
        }

        with mock.patch.object(hibernate, "transcript_age_minutes",
                              return_value=1), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value={}), \
                mock.patch.object(hibernate, "stub_alive", return_value=True), \
                mock.patch.object(hibernate, "remove_stub_file"), \
                mock.patch.object(hibernate, "write_stub_file"), \
                mock.patch.object(hibernate, "gc_stub_files"), \
                mock.patch.object(hibernate, "save_state"):
            hibernate.restore_records(
                [pane], {"tab-stable": "My project"}, state)

        self.assertNotIn("pane-old", state)
        self.assertEqual(
            state["pane-new"]["workspace_id"], "workspace-new")

    def test_wake_restores_exact_owned_marker(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "Review Orchestrator",
            "marker_added": True,
            "marked_label": "💤 Review Orchestrator",
        }
        state = {"w1:p1": rec}
        calls = []

        def fake_herdr(*args):
            calls.append(args)
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "💤 Review Orchestrator"}}
            return {}

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, state)

        self.assertTrue(ok)
        self.assertIn(
            ("tab", "rename", "w1:t1", "Review Orchestrator"), calls)

    def test_legitimate_sleeping_emoji_is_not_removed(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "💤 My own label",
            "marker_added": False,
            "marked_label": None,
        }
        with mock.patch.object(hibernate, "herdr") as herdr:
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, {"w1:p1": rec})

        self.assertTrue(ok)
        herdr.assert_not_called()

    def test_user_changed_label_is_left_untouched(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "Original",
            "marker_added": True,
            "marked_label": "💤 Original",
        }

        def fake_herdr(*args):
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "User renamed this"}}
            raise AssertionError("rename must not be called")

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr):
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, {"w1:p1": rec})

        self.assertTrue(ok)

    def test_multi_pane_tab_keeps_marker_until_last_pane_wakes(self):
        rec = {
            "tab_id": "w1:t1",
            "label": "Shared tab",
            "marker_added": True,
            "marked_label": "💤 Shared tab",
        }
        sibling = dict(rec)
        state = {"w1:p1": rec, "w1:p2": sibling}

        with mock.patch.object(hibernate, "herdr") as herdr:
            ok = hibernate.restore_owned_tab_label(
                "w1:p1", {"tab_id": "w1:t1"}, rec, state)

        self.assertTrue(ok)
        herdr.assert_not_called()

    def test_rename_failure_keeps_record_for_retry(self):
        rec = {
            "uuid": "90141d62-7130-4dd0-8083-211884e8e999",
            "agent": "claude",
            "tab_id": "w1:t1",
            "label": "Original",
            "marker_added": True,
            "marked_label": "💤 Original",
        }
        state = {"w1:p1": rec}
        pane = {
            "pane_id": "w1:p1",
            "tab_id": "w1:t1",
            "agent": "claude",
        }

        def fake_herdr(*args):
            if args[:2] == ("tab", "get"):
                return {"tab": {"label": "💤 Original"}}
            if args[:2] == ("tab", "rename"):
                raise RuntimeError("temporary failure")
            raise AssertionError("unexpected Herdr call: %r" % (args,))

        with mock.patch.object(hibernate, "herdr", side_effect=fake_herdr), \
                mock.patch.object(hibernate, "transcript_age_minutes",
                                  return_value=1), \
                mock.patch.object(hibernate, "pane_process_info",
                                  return_value={}), \
                mock.patch.object(hibernate, "gc_stub_files"), \
                mock.patch.object(hibernate, "log"):
            hibernate.restore_records(
                [pane], {"w1:t1": "💤 Original"}, state)

        self.assertIn("w1:p1", state)

    def test_second_pane_reuses_one_tab_marker(self):
        existing = {
            "tab_id": "w1:t1",
            "label": "Shared tab",
            "marker_added": True,
            "marked_label": "💤 Shared tab",
        }
        result = hibernate.marker_record(
            "💤 Shared tab", "w1:t1", {"w1:p1": existing})
        self.assertEqual(result, ("Shared tab", "💤 Shared tab", True))

    def test_stub_resets_terminal_before_banner_and_on_dismiss(self):
        rec = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "agent": "codex",
            "resume": ["codex", "resume",
                       "11111111-1111-1111-1111-111111111111"],
            "cwd": "/tmp",
            "freed_mb": 100,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        path = hibernate.write_stub_file("w1:p1", rec)
        with open(path, "r", encoding="utf-8") as fh:
            body = fh.read()

        self.assertIn("stty sane", body)
        self.assertIn("\\033[?1004l", body)
        self.assertIn("_hb_bye() {\n    _hb_reset_terminal", body)
        self.assertIn("export HERDR_HIBERNATE_STUB=1\n"
                      "    _hb_reset_terminal", body)
        self.assertNotIn("\n    clear", body)

    def test_arm_command_fixes_the_line_discipline_before_starting_stub(self):
        command = hibernate.stub_command("w1:p1")

        self.assertLess(command.index("stty sane"), command.index("bash "))
        self.assertNotIn("clear", command)

    def test_arm_command_carries_no_escape_noise_for_the_user_to_read(self):
        """It is typed into the pane, so the shell echoes every byte of it."""
        command = hibernate.stub_command("w1:p1")
        self.assertNotIn("\\033[", command)
        self.assertLessEqual(len(command.splitlines()), 1)

    def test_the_stub_itself_still_resets_the_emulator_modes(self):
        """Which is why the arm command does not have to."""
        rec = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "agent": "claude", "cwd": "/tmp", "freed_mb": 1,
            "resume": ["claude", "--resume",
                       "11111111-1111-1111-1111-111111111111"],
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        with open(hibernate.write_stub_file("w1:p1", rec), encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("\\033[?1004l", body)
        self.assertIn("export HERDR_HIBERNATE_STUB=1\n    _hb_reset_terminal", body)


class WatcherGuardTests(unittest.TestCase):
    """Guards that keep a watching orchestrator from being hibernated."""

    SID = "11111111-1111-1111-1111-111111111111"

    @staticmethod
    def _cfg(**over):
        cfg = dict(hibernate.DEFAULTS)
        cfg.update(over)
        return cfg

    @staticmethod
    def _write_jsonl(path, age_seconds):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S",
                              time.gmtime(time.time() - age_seconds))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"timestamp":"%s","type":"assistant"}\n' % stamp)

    def _claude_spec(self, root):
        spec = dict(hibernate.AGENTS["claude"])
        spec["transcript_glob"] = os.path.join(root, "{sid}.jsonl")
        spec["activity_globs"] = (
            os.path.join(root, "{sid}", "subagents", "*.jsonl"),)
        return spec

    def test_subagent_writes_count_as_session_activity(self):
        with tempfile.TemporaryDirectory() as root:
            self._write_jsonl(os.path.join(root, self.SID + ".jsonl"),
                              age_seconds=3 * 3600)  # main stale for 3h
            subdir = os.path.join(root, self.SID, "subagents")
            os.makedirs(subdir)
            self._write_jsonl(os.path.join(subdir, "agent-worker.jsonl"),
                              age_seconds=60)  # worker wrote a minute ago
            with mock.patch.dict(hibernate.AGENTS,
                                 {"claude": self._claude_spec(root)}):
                age = hibernate.transcript_age_minutes(self.SID, "claude")
            self.assertIsNotNone(age)
            self.assertLess(age, 10)

    def test_subagent_files_alone_do_not_make_a_session_resumable(self):
        with tempfile.TemporaryDirectory() as root:
            subdir = os.path.join(root, self.SID, "subagents")
            os.makedirs(subdir)
            self._write_jsonl(os.path.join(subdir, "agent-worker.jsonl"), 60)
            with mock.patch.dict(hibernate.AGENTS,
                                 {"claude": self._claude_spec(root)}):
                age = hibernate.transcript_age_minutes(self.SID, "claude")
            self.assertIsNone(age)  # no main transcript -> never killed anyway

    def test_late_started_child_is_a_busy_background_job(self):
        procs = {
            100: (1, 0, "claude"),
            101: (100, 0, "bash -c 'herdr wait agent-status w1:p2 --status done'"),
        }
        ups = {100: 120.0, 101: 4.0}  # child began ~116m after the agent
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "claude",
                                                self._cfg())
        self.assertIsNotNone(job)
        self.assertIn("herdr wait", job)

    def test_startup_children_and_mcp_servers_do_not_count(self):
        procs = {
            100: (1, 0, "claude"),
            101: (100, 0, "node /x/mcp-server-figma"),  # late but ignored token
            102: (100, 0, "node /x/some-helper"),       # started with the agent
        }
        ups = {100: 120.0, 101: 4.0, 102: 119.5}
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "claude",
                                                self._cfg())
        self.assertIsNone(job)

    def test_reexeced_agent_binary_is_not_a_background_job(self):
        # codex node wrapper (root) relaunched its vendored TUI 71m in; the
        # TUI carries the session id, its code-mode-host helper follows 26s
        # later. Neither is background work.
        procs = {
            100: (1, 0, "node /x/bin/codex resume %s -c model=y" % self.SID),
            101: (100, 0, "/x/vendor/bin/codex resume %s -c model=y" % self.SID),
            102: (101, 0, "/x/vendor/bin/codex-code-mode-host"),
        }
        ups = {100: 120.0, 101: 49.0, 102: 48.5}
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "codex",
                                                self._cfg())
        self.assertIsNone(job)

    def test_worker_spawned_after_a_reexec_still_counts(self):
        procs = {
            100: (1, 0, "node /x/bin/codex resume %s" % self.SID),
            101: (100, 0, "/x/vendor/bin/codex resume %s" % self.SID),
            102: (101, 0, "codex exec --session other 'review the diff'"),
        }
        ups = {100: 120.0, 101: 49.0, 102: 10.0}  # worker began 39m after re-exec
        with mock.patch.object(hibernate, "find_agent_proc",
                               return_value=(100, {})), \
                mock.patch.object(hibernate, "ps_snapshot", return_value=procs), \
                mock.patch.object(hibernate, "proc_uptime_minutes",
                                  side_effect=ups.get):
            job = hibernate.busy_background_job("w1:p1", self.SID, "codex",
                                                self._cfg())
        self.assertIsNotNone(job)
        self.assertIn("codex exec", job)

    def test_zero_minutes_disables_the_busy_check(self):
        with mock.patch.object(hibernate, "find_agent_proc") as finder:
            job = hibernate.busy_background_job(
                "w1:p1", self.SID, "claude",
                self._cfg(BUSY_CHILD_MINUTES="0"))
        self.assertIsNone(job)
        finder.assert_not_called()

    def test_classify_skips_a_pane_with_a_busy_background_job(self):
        pane = {
            "pane_id": "w1:p1", "tab_id": "w1:t1", "agent": "claude",
            "agent_status": "idle",
            "agent_session": {"value": self.SID},
        }
        with mock.patch.object(hibernate, "transcript_age_minutes",
                               return_value=999.0), \
                mock.patch.object(hibernate, "busy_background_job",
                                  return_value="herdr wait agent-status w1:p2"):
            decision, reason = hibernate.classify(
                pane, {"w1:t1": "KB Delta orchestrator"}, self._cfg(),
                {}, "w9:p9")
        self.assertEqual(decision, "skip")
        self.assertTrue(reason.startswith("busy:"))


class LastExchangeTests(unittest.TestCase):
    """The excerpt the hibernation stub reprints from an on-disk transcript."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = self.tempdir.name

    def tearDown(self):
        self.tempdir.cleanup()

    def write(self, agent, sid, entries):
        path = os.path.join(self.root, "%s.jsonl" % sid)
        with open(path, "w", encoding="utf-8") as fh:
            for entry in entries:
                fh.write(json.dumps(entry) + "\n")
        spec = dict(hibernate.AGENTS[agent])
        spec["transcript_glob"] = os.path.join(self.root, "{sid}.jsonl")
        return mock.patch.dict(hibernate.AGENTS, {agent: spec})

    @staticmethod
    def claude_user(text, **extra):
        entry = {"type": "user", "message": {"role": "user", "content": text}}
        entry.update(extra)
        return entry

    @staticmethod
    def claude_agent(text, **extra):
        entry = {"type": "assistant",
                 "message": {"role": "assistant",
                             "content": [{"type": "text", "text": text}]}}
        entry.update(extra)
        return entry

    @staticmethod
    def codex_message(role, text):
        key = "input_text" if role == "user" else "output_text"
        return {"type": "response_item",
                "payload": {"type": "message", "role": role,
                            "content": [{"type": key, "text": text}]}}

    def test_claude_pairs_the_newest_prompt_with_its_reply(self):
        with self.write("claude", "sid", [
            self.claude_user("older question"),
            self.claude_agent("older answer"),
            self.claude_user("why is staging returning 404"),
            self.claude_agent("The rewrite rule was wrong. Pushed a fix."),
        ]):
            self.assertEqual(
                hibernate.last_exchange("sid", "claude"),
                ("why is staging returning 404",
                 "The rewrite rule was wrong. Pushed a fix."))

    def test_unanswered_prompt_is_not_paired_with_an_older_reply(self):
        """Hibernating mid-turn must not attribute the previous answer."""
        with self.write("claude", "sid", [
            self.claude_user("first"),
            self.claude_agent("first answer"),
            self.claude_user("second, still running"),
        ]):
            self.assertEqual(hibernate.last_exchange("sid", "claude"),
                             ("second, still running", ""))

    def test_machine_authored_user_entries_are_skipped(self):
        """Hooks, tool results, sub-agents and compaction are not the human."""
        with self.write("claude", "sid", [
            self.claude_user("the real question"),
            self.claude_agent("the real answer"),
            self.claude_user("sidechain prompt", isSidechain=True),
            self.claude_agent("sidechain answer", isSidechain=True),
            self.claude_user("skill injection", isMeta=True),
            self.claude_user([{"type": "tool_result", "content": "ok"}],
                             toolUseResult={"ok": True}),
            self.claude_user("<task-notification>\n<status>done</status>"),
            self.claude_user("/compact"),
            self.claude_user(
                "This session is being continued from a previous conversation"),
        ]):
            self.assertEqual(hibernate.last_exchange("sid", "claude"),
                             ("the real question", "the real answer"))

    def test_codex_reads_its_own_rollout_shape(self):
        with self.write("codex", "sid", [
            {"type": "event_msg", "payload": {"type": "token_count"}},
            self.codex_message("developer", "system preamble"),
            self.codex_message("user", "add the retry guard"),
            self.codex_message("assistant", "Added it and committed."),
        ]):
            self.assertEqual(hibernate.last_exchange("sid", "codex"),
                             ("add the retry guard", "Added it and committed."))

    def test_a_prompt_buried_past_the_window_still_shows_the_reply(self):
        """Tool-heavy sessions can push every prompt out of the read window."""
        filler = {"type": "assistant",
                  "message": {"role": "assistant",
                              "content": [{"type": "tool_use", "id": "x" * 4000}]}}
        entries = [self.claude_user("buried")] + [filler] * 40 + [
            self.claude_agent("the surviving answer")]
        with self.write("claude", "sid", entries):
            with mock.patch.object(hibernate, "EXCERPT_TAIL_BYTES", 8 * 1024), \
                    mock.patch.object(hibernate, "EXCERPT_WIDE_TAIL_BYTES", 8 * 1024):
                self.assertEqual(hibernate.last_exchange("sid", "claude"),
                                 ("", "the surviving answer"))

    def test_widening_the_window_recovers_the_prompt(self):
        filler = {"type": "assistant",
                  "message": {"role": "assistant",
                              "content": [{"type": "tool_use", "id": "x" * 4000}]}}
        entries = [self.claude_user("buried but reachable")] + [filler] * 40 + [
            self.claude_agent("the answer")]
        with self.write("claude", "sid", entries):
            with mock.patch.object(hibernate, "EXCERPT_TAIL_BYTES", 8 * 1024):
                self.assertEqual(hibernate.last_exchange("sid", "claude"),
                                 ("buried but reachable", "the answer"))

    def test_turns_are_returned_whole(self):
        """Shortening is the display's business, and it does not shorten either."""
        with self.write("claude", "sid", [
            self.claude_user("word " * 400),
            self.claude_agent("reply " * 900),
        ]):
            user, said = hibernate.last_exchange("sid", "claude")
        self.assertEqual(user, ("word " * 400).strip())
        self.assertEqual(said, ("reply " * 900).strip())

    def test_an_agent_without_a_reader_yields_no_excerpt(self):
        """Grok has no verified reader yet; that must degrade, not raise."""
        self.assertNotIn("turn_reader", hibernate.AGENTS["grok"])
        self.assertEqual(hibernate.last_exchange("sid", "grok"), ("", ""))

    def test_a_missing_transcript_yields_no_excerpt(self):
        self.assertEqual(hibernate.last_exchange("nope", "claude"), ("", ""))


ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


class StubExcerptTests(unittest.TestCase):
    """Rendering the last exchange into the pane's stub script."""

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = self.tempdir.name
        self.paths = mock.patch.multiple(
            hibernate, CONFIG_DIR=self.root,
            PANES_DIR=os.path.join(self.root, "panes"))
        self.paths.start()
        self.rec = {
            "uuid": "11111111-1111-1111-1111-111111111111",
            "agent": "claude",
            "resume": ["claude", "--resume",
                       "11111111-1111-1111-1111-111111111111"],
            "cwd": "/tmp",
            "freed_mb": 100,
            "at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

    def tearDown(self):
        self.paths.stop()
        self.tempdir.cleanup()

    def render(self, user, said, columns="100", **overrides):
        """Write a stub with a fixed excerpt and run it, returning what it printed."""
        rec = dict(self.rec, **overrides)
        with mock.patch.object(hibernate, "last_exchange",
                               return_value=(user, said)):
            path = hibernate.write_stub_file("w1:p1", rec)
        # `bash -n` first: the excerpt is arbitrary human text interpolated into
        # a shell script, so a quoting slip would break the pane, not the pixels.
        syntax = subprocess.run(["bash", "-n", path], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        run = subprocess.run(["bash", path], stdin=subprocess.DEVNULL,
                             capture_output=True, text=True,
                             env=dict(os.environ, COLUMNS=columns, TERM="dumb"))
        return path, run.stdout

    def test_the_last_exchange_is_printed_above_the_banner(self):
        """The banner has to end up next to the cursor, under the excerpt."""
        _, out = self.render("why is staging 404ing",
                             "The rewrite rule was wrong. Pushed a fix.")

        self.assertIn("you", out)
        self.assertIn("claude", out)
        self.assertLess(out.index("why is staging 404ing"),
                        out.index("The rewrite rule was wrong."))
        self.assertLess(out.index("The rewrite rule was wrong."),
                        out.index("press Enter to resume"))

    def test_the_excerpt_is_dim(self):
        """Dim is the whole point: present, but not competing with live output."""
        _, out = self.render("a question", "an answer")
        self.assertIn("\x1b[2myou", out)
        self.assertIn("a question\x1b[0m", out)

    def test_shell_metacharacters_in_a_turn_cannot_escape(self):
        """Transcript text is untrusted input being written into a script."""
        nasty = "'; touch /tmp/hb-pwned; echo '$(id) `id` ${PATH} \\ \" #"
        _, out = self.render(nasty, "and $(hostname) too")

        self.assertFalse(os.path.exists("/tmp/hb-pwned"))
        self.assertIn("touch /tmp/hb-pwned", out)
        self.assertIn("$(id)", out)
        self.assertIn("$(hostname)", out)

    def test_a_long_reply_is_printed_whole_by_default(self):
        """No cut, no ellipsis: a half-quoted answer sends you into the pane."""
        reply = " ".join("sentence%d" % n for n in range(400))
        _, out = self.render("short", reply)

        plain = ANSI_RE.sub("", out)
        self.assertNotIn("…", plain)
        for word in ("sentence0", "sentence200", "sentence399"):
            self.assertIn(word, plain)
        self.assertGreater(len([line for line in plain.splitlines()
                                if "sentence" in line]), 10)

    def test_a_numeric_setting_still_caps_and_marks_the_cut(self):
        with mock.patch.object(hibernate, "_excerpt_lines", 3):
            _, out = self.render("short", "word " * 400)

        reply = [line for line in out.splitlines() if "word" in line]
        self.assertEqual(len(reply), 3)
        self.assertIn("…", out)

    def test_zero_hides_the_excerpt_entirely(self):
        with mock.patch.object(hibernate, "_excerpt_shown", False):
            with mock.patch.object(hibernate, "last_exchange") as reader:
                path = hibernate.write_stub_file("w1:p1", self.rec)
        reader.assert_not_called()
        with open(path, encoding="utf-8") as fh:
            body = fh.read()
        self.assertIn("_hb_turn 'you' '' ", body)

    def test_excerpt_lines_settings_map_to_show_and_cap(self):
        self.assertEqual(hibernate._parse_excerpt_lines("all"), (True, 0))
        self.assertEqual(hibernate._parse_excerpt_lines("6"), (True, 6))
        self.assertEqual(hibernate._parse_excerpt_lines("0"), (False, 0))
        # A typo must not silently swallow the conversation.
        self.assertEqual(hibernate._parse_excerpt_lines("yes please"), (True, 0))
        self.assertEqual(hibernate._parse_excerpt_lines(None), (True, 0))

    def test_a_session_with_no_readable_exchange_prints_only_the_banner(self):
        _, out = self.render("", "")
        self.assertIn("hibernated", out)
        self.assertNotIn("you ", out)

    def test_a_long_agent_name_does_not_break_the_label_column(self):
        _, out = self.render("QQQ", "AAA", agent_name="worker-codex")
        plain = [ANSI_RE.sub("", line) for line in out.splitlines()]
        columns = {line.index(word) for line, word in
                   ((row, "QQQ") for row in plain if "QQQ" in row)}
        columns |= {line.index("AAA") for line in plain if "AAA" in line}
        self.assertEqual(len(columns), 1, plain)

    def test_the_stub_is_owner_only(self):
        """It holds conversation text now, and transcripts carry pasted keys."""
        path, _ = self.render("a question", "an answer")
        self.assertEqual(oct(os.stat(path).st_mode)[-3:], "700")
        self.assertEqual(oct(os.stat(hibernate.PANES_DIR).st_mode)[-3:], "700")


if __name__ == "__main__":
    unittest.main()
