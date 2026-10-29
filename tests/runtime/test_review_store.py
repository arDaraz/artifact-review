#!/usr/bin/env python3
"""Contract tests for the incremental, durable review session store."""

import copy
import json
import pathlib
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest import mock


ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRIPTS = ROOT / "skills" / "artifact-review" / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from review_store import QuotaExceeded, ReviewStore
import server as SERVER
from versioning import STATE_SCHEMA


def sample_state():
    return {
        "ended": False,
        "ended_by": None,
        "queue": [
            {"qid": "queue-1", "kind": "chat", "text": "First", "ts": 1.0},
            {"qid": "queue-2", "kind": "chat", "text": "Second", "ts": 2.0},
        ],
        "audit": {"status": "clear", "findings": []},
        "feed": [
            {
                "id": "feedback-1",
                "role": "human",
                "ts": 3.0,
                "items": [{"qid": "sent-1", "kind": "chat", "text": "Sent"}],
                "status": "sent",
            },
            {
                "id": "reply-1",
                "role": "agent",
                "ts": 4.0,
                "text": "Reply",
                "reply_to": "feedback-1",
            },
        ],
        "events": [
            {
                "schema": "artifact-review/event/v1",
                "id": "feedback-1",
                "type": "feedback",
                "items": [{"qid": "sent-1", "kind": "chat", "text": "Sent"}],
                "sent_at": 3.0,
            }
        ],
        "agent": {"status": "idle", "last_seen": 5.0},
        "owner": {"agent": "claude-code",
                  "session_id": "37959434-8e03-4050-b5a3-b40f776fd998",
                  "cwd": "/tmp", "pid": None},
    }


class ReviewStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.session_dir = pathlib.Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def open_store(self):
        return ReviewStore(str(self.session_dir), STATE_SCHEMA)

    def table_count(self, table):
        with sqlite3.connect(self.session_dir / "review.sqlite3") as connection:
            return connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]

    def test_creates_hardened_schema_and_normalized_rows(self):
        store = self.open_store()
        try:
            store.sync(sample_state())
            pragmas = {
                "journal_mode": store._conn.execute("PRAGMA journal_mode").fetchone()[0],
                "synchronous": store._conn.execute("PRAGMA synchronous").fetchone()[0],
                "foreign_keys": store._conn.execute("PRAGMA foreign_keys").fetchone()[0],
            }
        finally:
            store.close()

        self.assertEqual(pragmas["journal_mode"].lower(), "wal")
        self.assertEqual(pragmas["synchronous"], 2)
        self.assertEqual(pragmas["foreign_keys"], 1)
        self.assertEqual(self.table_count("queue_items"), 2)
        self.assertEqual(self.table_count("feed_items"), 2)
        self.assertEqual(self.table_count("agent_events"), 1)
        self.assertEqual(self.table_count("migrations"), 1)

    def test_sync_appends_updates_and_deletes_only_normalized_rows(self):
        original = sample_state()
        store = self.open_store()
        try:
            store.sync(original)
            changed = copy.deepcopy(original)
            changed["queue"] = [changed["queue"][1], {
                "qid": "queue-3", "kind": "chat", "text": "Third", "ts": 6.0,
            }]
            changed["feed"][0]["status"] = "delivered"
            changed["feed"].append({
                "id": "reply-2", "role": "agent", "ts": 7.0,
                "text": "Another reply", "reply_to": "feedback-1",
            })
            changed["events"] = []
            changed["ended"] = True
            changed["ended_by"] = "user"
            store.sync(changed)
            restored = store.load()
        finally:
            store.close()

        self.assertEqual(restored, changed)
        self.assertEqual(self.table_count("queue_items"), 2)
        self.assertEqual(self.table_count("feed_items"), 3)
        self.assertEqual(self.table_count("agent_events"), 0)

    def test_sync_rolls_back_every_table_when_one_write_fails(self):
        original = sample_state()
        store = self.open_store()
        try:
            store.sync(original)
            store._conn.execute(
                "CREATE TRIGGER reject_event BEFORE INSERT ON agent_events "
                "BEGIN SELECT RAISE(ABORT, 'forced failure'); END"
            )
            changed = copy.deepcopy(original)
            changed["queue"].append({
                "qid": "queue-3", "kind": "chat", "text": "Third", "ts": 6.0,
            })
            changed["events"].append({
                "schema": "artifact-review/event/v1", "id": "feedback-2",
                "type": "feedback", "items": [], "sent_at": 7.0,
            })
            with self.assertRaises(sqlite3.DatabaseError):
                store.sync(changed)
            self.assertEqual(store.load(), original)
        finally:
            store.close()

    def test_committed_state_is_immediately_visible_to_concurrent_reader(self):
        store = self.open_store()
        reader = self.open_store()
        ready = threading.Event()
        observed = []

        def read_after_commit():
            ready.wait(timeout=2)
            observed.append(reader.load())

        thread = threading.Thread(target=read_after_commit)
        thread.start()
        try:
            expected = sample_state()
            store.sync(expected)
            ready.set()
            thread.join(timeout=2)
            self.assertFalse(thread.is_alive())
            self.assertEqual(observed, [expected])
        finally:
            store.close()
            reader.close()

    def test_valid_legacy_json_migrates_once_without_loss(self):
        expected = sample_state()
        legacy = self.session_dir / "session.json"
        legacy.write_text(json.dumps(expected), encoding="utf-8")

        store = self.open_store()
        try:
            self.assertEqual(store.load(), expected)
        finally:
            store.close()

        self.assertFalse(legacy.exists())
        self.assertTrue((self.session_dir / "session.legacy.json").is_file())
        self.assertEqual(self.table_count("migrations"), 2)

    def test_invalid_legacy_json_is_quarantined_not_overwritten(self):
        legacy = self.session_dir / "session.json"
        legacy.write_text("{broken", encoding="utf-8")

        store = self.open_store()
        try:
            self.assertEqual(store.load(), ReviewStore.empty_state())
        finally:
            store.close()

        quarantined = list(self.session_dir.glob("session.corrupt.*.json"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_text(encoding="utf-8"), "{broken")

    def test_corrupt_sqlite_is_quarantined_before_recovery(self):
        database = self.session_dir / "review.sqlite3"
        database.write_bytes(b"not a sqlite database")

        store = self.open_store()
        try:
            store.sync(sample_state())
            self.assertEqual(store.load(), sample_state())
        finally:
            store.close()

        quarantined = list(self.session_dir.glob("review.corrupt.*.sqlite3"))
        self.assertEqual(len(quarantined), 1)
        self.assertEqual(quarantined[0].read_bytes(), b"not a sqlite database")

    def test_restart_load_is_equivalent_and_activity_is_stably_paged(self):
        expected = sample_state()
        expected["feed"] = [
            {"id": f"feed-{index}", "role": "agent", "ts": float(index),
             "text": f"Reply {index}"}
            for index in range(125)
        ]
        store = self.open_store()
        store.sync(expected)
        store.close()

        reopened = self.open_store()
        try:
            self.assertEqual(reopened.load(), expected)
            newest = reopened.activity(before=None, limit=50)
            earlier = reopened.activity(before=newest["next_before"], limit=50)
            oldest = reopened.activity(before=earlier["next_before"], limit=50)
        finally:
            reopened.close()

        self.assertEqual([item["id"] for item in newest["items"]],
                         [f"feed-{index}" for index in range(75, 125)])
        self.assertEqual([item["id"] for item in earlier["items"]],
                         [f"feed-{index}" for index in range(25, 75)])
        self.assertEqual([item["id"] for item in oldest["items"]],
                         [f"feed-{index}" for index in range(25)])
        self.assertEqual(newest["total"], 125)
        self.assertTrue(newest["has_more"])
        self.assertFalse(oldest["has_more"])

    def test_fixed_quotas_accept_the_boundary_and_describe_overflow(self):
        SERVER._require_quota("queue_items", 500, 500)
        SERVER._require_quota("pending_events", 1000, 1000)
        with self.assertRaises(QuotaExceeded) as raised:
            SERVER._require_quota("pending_events", 1001, 1000)
        self.assertEqual(raised.exception.payload(), {
            "error": "pending_events limit exceeded",
            "resource": "pending_events",
            "limit": 1000,
            "current": 1001,
        })

        previous_state = copy.deepcopy(SERVER.STATE)
        previous_feed_limit = SERVER.MAX_FEED_ITEMS
        try:
            SERVER.STATE["queue"] = [{
                "qid": "waiting", "kind": "chat", "text": "keep me",
            }]
            SERVER.STATE["events"] = [
                {"id": f"event-{index}", "type": "feedback"}
                for index in range(1000)
            ]
            with self.assertRaises(QuotaExceeded):
                SERVER._feedback_event_locked()
            self.assertEqual(SERVER.STATE["queue"][0]["qid"], "waiting")

            SERVER.MAX_FEED_ITEMS = 2
            SERVER.STATE["feed"] = [{"id": "one"}, {"id": "two"}]
            SERVER._append_feed_locked({"id": "three"})
            self.assertEqual(
                [item["id"] for item in SERVER.STATE["feed"]],
                ["two", "three"],
            )
        finally:
            SERVER.STATE.clear()
            SERVER.STATE.update(previous_state)
            SERVER.MAX_FEED_ITEMS = previous_feed_limit

    def test_feed_retention_keeps_newest_rows_and_records_trimmed_count(self):
        store = self.open_store()
        try:
            state = sample_state()
            store.sync(state)
            state["feed"] = state["feed"][1:]
            store.sync(state)
            self.assertEqual(store.load()["feed"], state["feed"])
            self.assertEqual(store.usage()["feed_trimmed"], 1)
        finally:
            store.close()

    def test_snapshots_deduplicate_enforce_limits_and_prune_by_reference(self):
        store = self.open_store()
        previous = {
            "session": SERVER.SESSION_DIR,
            "store": SERVER.STORE,
            "blob_limit": SERVER.MAX_SNAPSHOT_BLOBS,
            "byte_limit": SERVER.MAX_SNAPSHOT_BYTES,
        }
        SERVER.SESSION_DIR = str(self.session_dir)
        SERVER.STORE = store
        png = b"\x89PNG\r\n\x1a\n" + b"preview" * 20
        try:
            first = SERVER._save_whiteboard_blobs_locked(
                {"type": "excalidraw", "elements": [{"id": "one"}]}, png)
            duplicate = SERVER._save_whiteboard_blobs_locked(
                {"elements": [{"id": "one"}], "type": "excalidraw"}, png)
            different = SERVER._save_whiteboard_blobs_locked(
                {"type": "excalidraw", "elements": [{"id": "two"}]}, png)
            self.assertEqual(first, duplicate)
            self.assertNotEqual(first["scene_path"], different["scene_path"])
            self.assertEqual(first["png_path"], different["png_path"])
            self.assertEqual(store.usage()["snapshot_blobs"], 3)

            SERVER.MAX_SNAPSHOT_BLOBS = 3
            SERVER._save_whiteboard_blobs_locked(
                {"type": "excalidraw", "elements": [{"id": "one"}]}, png)
            with self.assertRaises(QuotaExceeded) as raised:
                SERVER._save_whiteboard_blobs_locked(
                    {"type": "excalidraw", "elements": [{"id": "three"}]}, png)
            self.assertEqual(raised.exception.resource, "snapshot_blobs")
            self.assertEqual(raised.exception.current, 4)

            state = ReviewStore.empty_state()
            state["queue"] = [{
                "qid": "keep-blob",
                "kind": "whiteboard",
                "scene_path": first["scene_path"],
                "png_path": first["png_path"],
                "scene_hash": first["scene_hash"],
                "png_hash": first["png_hash"],
            }]
            store.sync(state)
            pruned = store.prune_unreferenced_blobs()
            self.assertEqual(pruned["removed_count"], 1)
            self.assertTrue(pathlib.Path(first["scene_path"]).is_file())
            self.assertTrue(pathlib.Path(first["png_path"]).is_file())
            self.assertFalse(pathlib.Path(different["scene_path"]).exists())
        finally:
            SERVER.SESSION_DIR = previous["session"]
            SERVER.STORE = previous["store"]
            SERVER.MAX_SNAPSHOT_BLOBS = previous["blob_limit"]
            SERVER.MAX_SNAPSHOT_BYTES = previous["byte_limit"]
            store.close()

    def test_partial_snapshot_failure_removes_new_blobs_and_temps(self):
        store = self.open_store()
        previous_session = SERVER.SESSION_DIR
        previous_store = SERVER.STORE
        SERVER.SESSION_DIR = str(self.session_dir)
        SERVER.STORE = store
        original_write = SERVER._write_blob_atomic
        calls = 0

        def fail_second_write(path, value, digest):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("forced PNG failure")
            return original_write(path, value, digest)

        try:
            with mock.patch.object(
                    SERVER, "_write_blob_atomic", side_effect=fail_second_write):
                with self.assertRaises(OSError):
                    SERVER._save_whiteboard_blobs_locked(
                        {"type": "excalidraw", "elements": [{"id": "partial"}]},
                        b"\x89PNG\r\n\x1a\npartial",
                    )
            blob_dir = self.session_dir / "whiteboards" / "blobs"
            self.assertEqual(list(blob_dir.iterdir()), [])
        finally:
            SERVER.SESSION_DIR = previous_session
            SERVER.STORE = previous_store
            store.close()

    def test_ending_a_review_discards_unsent_editor_drafts(self):
        previous_session = SERVER.SESSION_DIR
        SERVER.SESSION_DIR = str(self.session_dir)
        try:
            directory = pathlib.Path(SERVER._whiteboard_dir_locked())
            draft = directory / "process-topology.working.json"
            draft.write_text("{}", encoding="utf-8")
            blob = pathlib.Path(SERVER._whiteboard_blob_dir_locked()) / "a.excalidraw"
            blob.write_text("{}", encoding="utf-8")

            SERVER._discard_working_whiteboards_locked()

            self.assertFalse(draft.exists())
            self.assertTrue(blob.is_file())
        finally:
            SERVER.SESSION_DIR = previous_session


if __name__ == "__main__":
    unittest.main()
