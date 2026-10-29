#!/usr/bin/env python3
"""Contract tests for reopening the agent session from the review."""

import os
import pathlib
import sys
import tempfile
import time
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "artifact-review" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import server as SERVER


SESSION_ID = "37959434-8e03-4050-b5a3-b40f776fd998"


class ResumeRefusalTests(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(SERVER.STATE["agent"]), SERVER.RESUME_LAUNCHED_AT)
        SERVER.STATE["agent"] = {"status": "offline", "last_seen": None}
        SERVER.RESUME_LAUNCHED_AT = 0.0
        self.owner = {"agent": "claude-code", "session_id": SESSION_ID,
                      "cwd": str(ROOT), "pid": None}

    def tearDown(self):
        SERVER.STATE["agent"], SERVER.RESUME_LAUNCHED_AT = self.saved

    def test_allows_a_recorded_session_when_no_agent_polls(self):
        self.assertIsNone(SERVER._resume_refusal_locked(self.owner))

    def test_refuses_without_a_recorded_session(self):
        self.assertIn("no agent session", SERVER._resume_refusal_locked(None))

    def test_refuses_while_an_agent_polls(self):
        SERVER.STATE["agent"] = {"status": "listening", "last_seen": time.time()}
        self.assertIn("already polling",
                      SERVER._resume_refusal_locked(self.owner))

    def test_allows_when_the_last_poll_is_stale(self):
        SERVER.STATE["agent"] = {
            "status": "listening",
            "last_seen": time.time() - SERVER.AGENT_LIVE_SECONDS - 1,
        }
        self.assertIsNone(SERVER._resume_refusal_locked(self.owner))

    def test_refuses_a_second_launch_during_the_cooldown(self):
        SERVER.RESUME_LAUNCHED_AT = time.time()
        self.assertIn("already starting",
                      SERVER._resume_refusal_locked(self.owner))

    def test_refuses_a_session_whose_process_still_runs(self):
        self.owner["pid"] = os.getpid()
        self.assertIn("still open", SERVER._resume_refusal_locked(self.owner))


class OwnerValidationTests(unittest.TestCase):
    def owner(self, **changes):
        body = {"agent": "claude-code", "session_id": SESSION_ID,
                "cwd": str(ROOT), "pid": 42}
        return SERVER._normalise_owner(dict(body, **changes))

    def test_accepts_a_claude_code_session(self):
        self.assertEqual(self.owner()["session_id"], SESSION_ID)

    def test_rejects_another_agent(self):
        with self.assertRaises(ValueError):
            self.owner(agent="codex")

    def test_rejects_a_session_id_that_is_not_a_uuid(self):
        with self.assertRaises(ValueError):
            self.owner(session_id="abc; rm -rf ~")

    def test_rejects_a_missing_directory(self):
        with self.assertRaises(ValueError):
            self.owner(cwd=str(ROOT / "missing"))

    def test_rejects_a_boolean_pid(self):
        with self.assertRaises(ValueError):
            self.owner(pid=True)


class ResumeCommandTests(unittest.TestCase):
    def test_quotes_the_directory_and_prompt_for_the_shell(self):
        with mock.patch.object(SERVER, "ARTIFACT", "/tmp/it's here.html"):
            command = SERVER._resume_command({
                "session_id": SESSION_ID, "cwd": "/tmp/a dir"})
        self.assertTrue(command.startswith(
            f"cd '/tmp/a dir' && claude --resume {SESSION_ID} '"))
        self.assertIn("it'\"'\"'s here.html", command)

    def test_opens_a_private_command_file_on_macos(self):
        with tempfile.TemporaryDirectory() as session_dir, \
                mock.patch.object(SERVER, "SESSION_DIR", session_dir), \
                mock.patch.object(SERVER.sys, "platform", "darwin"), \
                mock.patch.object(SERVER.subprocess, "run") as run:
            self.assertTrue(SERVER._open_terminal('echo "hi"'))
            script = run.call_args.args[0][1]
            self.assertEqual(run.call_args.args[0][0], "open")
            self.assertEqual(pathlib.Path(script).read_text(),
                             '#!/bin/sh\necho "hi"\n')
            self.assertEqual(os.stat(script).st_mode & 0o777, 0o700)

    def test_reports_no_launch_outside_macos(self):
        with mock.patch.object(SERVER.sys, "platform", "linux"), \
                mock.patch.object(SERVER.subprocess, "run") as run:
            self.assertFalse(SERVER._open_terminal("true"))
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
