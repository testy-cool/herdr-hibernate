import importlib.machinery
import importlib.util
import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
