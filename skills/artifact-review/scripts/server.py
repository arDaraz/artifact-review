#!/usr/bin/env python3
"""
One-process-per-artifact HTTP server used by the ``arev`` CLI.

The controller document at ``/`` owns all authenticated state and mutation
requests. The reviewed document at ``/artifact`` receives an injected,
tokenless ``/sdk.js`` and is expected to run in a sandboxed iframe. Those
assets and the static nested whiteboard frame are intentionally tokenless so
reviewed content never learns the controller token; Host validation still
applies. Every controller document, state request, and mutation requires the
per-session token. CORS is enabled only for the static nested-editor assets.

Agent consumers long-poll ``GET /next``. Feedback is persisted before waiters
are notified, leased until acknowledged, and replayed after a failed consumer.
The bind address defaults to loopback but may be changed explicitly for a
user-managed port forwarder. JSON requests are capped at 32 MiB and decoded
whiteboard PNGs at 20 MiB. Pure standard library; prints ``LISTENING <port>``
after binding successfully.
"""

import argparse
import base64
import binascii
import copy
import glob
import gzip
import hashlib
import html
import json
import os
import re
import secrets
import shlex
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from review_store import QuotaExceeded, ReviewStore
from versioning import EVENT_SCHEMA, STATE_SCHEMA, TOOL_VERSION, event_envelope

VERSION = TOOL_VERSION
INSTANCE_ID = str(uuid.uuid4())
MAX_REQUEST_BYTES = 32 * 1024 * 1024
MAX_WHITEBOARD_PNG_BYTES = 20 * 1024 * 1024
MAX_WHITEBOARD_ID_LENGTH = 128
MAX_QUEUE_ITEMS = 500
MAX_PENDING_EVENTS = 1000
MAX_FEED_ITEMS = 10000
MAX_SNAPSHOT_BLOBS = 1000
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
END_SHUTDOWN_DELAY = 300.0
MAX_WATCHED_SHUTDOWN_DELAY = 3600.0
PUBLIC_REVIEW_PATHS = frozenset((
    "/artifact", "/sdk.js",
    "/whiteboard-frame", "/whiteboard.js", "/whiteboard.css",
    "/mermaid.js",
    # The browser asks for the favicon on its own and never carries the token.
    "/favicon.ico",
))
WHITEBOARD_ID_RE = re.compile(
    rf"^[a-zA-Z0-9_-]{{1,{MAX_WHITEBOARD_ID_LENGTH}}}$")
SOURCE_HASH_RE = re.compile(r"^[0-9a-f]{16,64}$")
MERMAID_NODE_TARGET_LIMITS = {
    "diagramId": 256,
    "nodeId": 256,
    "label": 512,
    "selector": 2048,
}
FEEDBACK_ITEM_KINDS = (
    "text", "element", "control", "chat", "whiteboard", "text-edit")
MAX_TEXT_EDIT_BLOCKS = 200
MAX_TEXT_EDIT_CHARS = 100_000
# Matches the browser: a status older than this means no agent is running.
AGENT_LIVE_SECONDS = 125
# A resumed agent needs time to start and poll, so one click opens one window.
RESUME_COOLDOWN_SECONDS = 60

STATE_LOCK = threading.Lock()
EVENTS_COND = threading.Condition(STATE_LOCK)

ARTIFACT = None          # absolute path of the reviewed file
SESSION_DIR = None
STORE = None
TOKEN = None
ASSET_DIR = None
ASSET_CACHE = {}
ASSET_HASHES = {}
ASSET_COMPRESSION_LOCK = threading.Lock()
ALLOWED_HOSTS = {"127.0.0.1", "localhost", "::1"}
STATE = {
    "version": 0,          # artifact file mtime_ns; chrome reloads on change
    "revision": 0,         # any state change; browser long-polls this value
    "ended": False,
    "ended_by": None,
    "queue": [],           # pending feedback items, not yet flushed
    "audit": {"status": "pending", "findings": []},
    "feed": [],            # chrome-visible history: flushes + agent replies
    "events": [],          # durable events waiting for an agent poll + ack
    "agent": {"status": "offline", "last_seen": None},
    "owner": None,         # the agent session that Resume reopens
    "warned": [],          # severe findings already attached to an event
}
REVISION_HISTORY = deque(maxlen=256)
PUBLISHED_STATE = None
SHUTDOWN_TIMER = None
# Only the review page polls for state, so this dates the last open browser tab.
# A request thread writes it without the lock: a float assignment is atomic and
# the reader only needs it to the nearest poll.
LAST_PAGE_POLL = 0.0
RESUME_LAUNCHED_AT = 0.0

MIME = {".js": "application/javascript", ".mjs": "application/javascript",
        ".css": "text/css", ".html": "text/html", ".svg": "image/svg+xml",
        ".png": "image/png", ".json": "application/json",
        ".woff": "font/woff", ".woff2": "font/woff2", ".ttf": "font/ttf",
        ".wasm": "application/wasm"}

REQUIRED_ASSETS = (
    "audit.js",
    "chrome.html",
    "favicon.svg",
    "sdk.js",
    "whiteboard-frame.html",
    "whiteboard.js",
    "whiteboard.css",
    "mermaid.js",
)


def _asset_entry(body, content_type):
    digest = hashlib.sha256(body).hexdigest()
    return {
        "body": body,
        "gzip": None,
        "content_type": content_type,
        "hash": digest,
        "etag": '"' + digest + '"',
        "gzip_etag": None,
    }


def _gzip_variant(entry):
    if entry["gzip"] is None:
        with ASSET_COMPRESSION_LOCK:
            if entry["gzip"] is None:
                compressed = gzip.compress(
                    entry["body"], compresslevel=6, mtime=0)
                entry["gzip"] = compressed
                entry["gzip_etag"] = (
                    '"' + hashlib.sha256(compressed).hexdigest() + '"')
    return entry["gzip"], entry["gzip_etag"]


def _load_asset_cache(asset_dir):
    raw = {}
    for name in REQUIRED_ASSETS:
        path = os.path.join(asset_dir, name)
        with open(path, "rb") as handle:
            raw[name] = handle.read()

    cache = {
        "whiteboard.js": _asset_entry(
            raw["whiteboard.js"], "application/javascript"),
        "whiteboard.css": _asset_entry(raw["whiteboard.css"], "text/css"),
        "mermaid.js": _asset_entry(
            raw["mermaid.js"], "application/javascript"),
        "favicon.svg": _asset_entry(raw["favicon.svg"], "image/svg+xml"),
    }
    sdk = raw["audit.js"] + b"\n" + raw["sdk.js"]
    cache["sdk.js"] = _asset_entry(sdk, "application/javascript")

    frame = raw["whiteboard-frame.html"]
    frame = frame.replace(
        b'href="/whiteboard.css"',
        ('href="/whiteboard.css?v=' + cache["whiteboard.css"]["hash"] + '"')
        .encode("ascii"),
    )
    frame = frame.replace(
        b'src="/whiteboard.js"',
        ('src="/whiteboard.js?v=' + cache["whiteboard.js"]["hash"] + '"')
        .encode("ascii"),
    )
    cache["whiteboard-frame"] = _asset_entry(
        frame, "text/html; charset=utf-8")
    cache["chrome.html"] = _asset_entry(
        raw["chrome.html"], "text/html; charset=utf-8")
    return cache


def _asset_url(name):
    entry = ASSET_CACHE[name]
    return f"/{name}?v={entry['hash']}"


def _persist_locked():
    STORE.sync({
        key: STATE[key]
        for key in ("ended", "ended_by", "queue", "audit", "feed",
                    "events", "agent", "owner")
    })


def _restore():
    """Restore durable feedback, but reset process/session-transient state."""
    saved = STORE.load()
    for key in ("queue", "feed"):
        if isinstance(saved.get(key), list):
            STATE[key] = saved[key]
    if len(STATE["feed"]) > MAX_FEED_ITEMS:
        STATE["feed"] = STATE["feed"][-MAX_FEED_ITEMS:]
    restored_events = saved.get("events")
    if isinstance(restored_events, list):
        # Feedback remains durable. Layout and ended events describe the prior
        # process lifecycle and must be regenerated for the new process.
        STATE["events"] = [
            event for event in restored_events
            if isinstance(event, dict) and event.get("type") == "feedback"
        ]
        for event in STATE["events"]:
            event.setdefault("schema", EVENT_SCHEMA)
            # A process restart invalidates any old delivery lease.
            event.pop("claimed_at", None)
    previous_agent = saved.get("agent")
    last_seen = (previous_agent.get("last_seen")
                 if isinstance(previous_agent, dict) else None)
    STATE["agent"] = {"status": "offline", "last_seen": last_seen}
    STATE["owner"] = saved.get("owner")
    # Every explicit server start is a reopened, live session that must audit
    # the current artifact. Persist this reset so another CLI sees it too.
    STATE["ended"] = False
    STATE["ended_by"] = None
    STATE["audit"] = {"status": "pending", "findings": []}
    _persist_locked()


def _changed_locked():
    global PUBLISHED_STATE
    previous = PUBLISHED_STATE or _state_locked()
    STATE["revision"] += 1
    current = _state_locked()
    changes = {}
    for key in ("version", "ended", "ended_by", "queue", "audit", "agent",
                "resumable", "activity"):
        if current[key] != previous.get(key):
            changes[key] = copy.deepcopy(current[key])
    previous_feed = {item.get("id"): item for item in previous.get("feed", [])}
    upserts = [
        copy.deepcopy(item) for item in current["feed"]
        if previous_feed.get(item.get("id")) != item
    ]
    if upserts:
        changes["feed_upserts"] = upserts
    REVISION_HISTORY.append({
        "revision": STATE["revision"],
        "changes": changes,
    })
    PUBLISHED_STATE = current
    EVENTS_COND.notify_all()


def _state_locked():
    if STORE is None:
        activity = {
            "items": STATE["feed"][-50:],
            "total": len(STATE["feed"]),
            "next_before": None,
            "has_more": len(STATE["feed"]) > 50,
        }
    else:
        activity = STORE.activity(before=None, limit=50)
    public = {
        key: STATE[key]
        for key in ("version", "revision", "ended", "ended_by", "queue",
                    "audit", "agent")
    }
    public["resumable"] = STATE["owner"] is not None
    public["feed"] = activity["items"]
    public["activity"] = {
        key: activity[key]
        for key in ("total", "next_before", "has_more")
    }
    return copy.deepcopy(public)


def _reset_publication_locked():
    global PUBLISHED_STATE
    REVISION_HISTORY.clear()
    PUBLISHED_STATE = _state_locked()


def _normalise_owner(body):
    if body.get("agent") != "claude-code":
        raise ValueError("unsupported agent")
    try:
        session_id = str(uuid.UUID(str(body.get("session_id"))))
    except ValueError:
        raise ValueError("session_id must be a UUID") from None
    cwd = body.get("cwd")
    if not isinstance(cwd, str) or not os.path.isabs(cwd) \
            or not os.path.isdir(cwd):
        raise ValueError("cwd must be an existing absolute directory")
    pid = body.get("pid")
    if pid is not None and (type(pid) is not int or pid <= 0):
        raise ValueError("pid must be a positive integer")
    return {"agent": "claude-code", "session_id": session_id, "cwd": cwd,
            "pid": pid}


def _process_running(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but belongs to another user.
        return True
    return True


def _resume_refusal_locked(owner):
    agent = STATE["agent"]
    if not owner:
        return "no agent session is recorded for this review"
    if (agent.get("status") in ("listening", "working")
            and time.time() - (agent.get("last_seen") or 0)
            <= AGENT_LIVE_SECONDS):
        return "an agent is already polling this review"
    if time.time() - RESUME_LAUNCHED_AT < RESUME_COOLDOWN_SECONDS:
        return "the agent session is already starting"
    if _process_running(owner.get("pid")):
        return ("the agent session is still open in another terminal. "
                "Switch to it and ask it to poll.")
    return None


def _resume_command(owner):
    prompt = (f"Feedback is waiting on the artifact-review board for "
              f"{ARTIFACT}. Use the artifact-review skill: open and poll that "
              f"file, apply the feedback, reply, and keep polling.")
    return (f"cd {shlex.quote(owner['cwd'])} && claude --resume "
            f"{owner['session_id']} {shlex.quote(prompt)}")


def _open_terminal(command):
    """Run the command in a new macOS terminal window.

    Return False when no window opened, so the reviewer gets the command.

    Opening a .command file needs no Automation permission, unlike AppleScript,
    and it uses the reviewer's own handler for .command files.
    """
    if sys.platform != "darwin":
        return False
    script = os.path.join(SESSION_DIR, "resume-agent.command")
    try:
        with open(script, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\n{command}\n")
        os.chmod(script, 0o700)
        subprocess.run(["open", script], check=True, capture_output=True,
                       timeout=15)
    except (OSError, subprocess.SubprocessError):
        return False
    return True


def _cancel_shutdown_locked():
    global SHUTDOWN_TIMER
    timer = SHUTDOWN_TIMER
    SHUTDOWN_TIMER = None
    if timer is not None:
        timer.cancel()


def _schedule_shutdown_locked(server, deadline=None):
    global SHUTDOWN_TIMER
    _cancel_shutdown_locked()
    scheduled_instance = INSTANCE_ID
    # A tab left open overnight must not hold the port until morning.
    if deadline is None:
        deadline = time.time() + MAX_WATCHED_SHUTDOWN_DELAY
    timer = None

    def shutdown_if_still_ended():
        global SHUTDOWN_TIMER
        with STATE_LOCK:
            if (SHUTDOWN_TIMER is not timer
                    or not STATE["ended"]
                    or INSTANCE_ID != scheduled_instance):
                return
            # A reviewer still has the page open. Taking the server away under
            # an open tab loses every edit made from that moment on.
            now = time.time()
            if now - LAST_PAGE_POLL < END_SHUTDOWN_DELAY and now < deadline:
                _schedule_shutdown_locked(server, deadline)
                return
            SHUTDOWN_TIMER = None
        server.shutdown()

    delay = min(END_SHUTDOWN_DELAY, max(0.0, deadline - time.time()))
    timer = threading.Timer(delay, shutdown_if_still_ended)
    timer.daemon = True
    SHUTDOWN_TIMER = timer
    timer.start()


def _delta_since_locked(after):
    revision = STATE["revision"]
    if after > revision or after < 0:
        return {"mode": "reset", "revision": revision,
                "state": _state_locked()}
    if after == revision:
        return {"mode": "delta", "revision": revision, "changes": {}}
    if not REVISION_HISTORY or after < REVISION_HISTORY[0]["revision"] - 1:
        return {"mode": "reset", "revision": revision,
                "state": _state_locked()}

    merged = {}
    feed_upserts = {}
    for record in REVISION_HISTORY:
        if record["revision"] <= after:
            continue
        for key, value in record["changes"].items():
            if key == "feed_upserts":
                for item in value:
                    feed_upserts[item["id"]] = copy.deepcopy(item)
            else:
                merged[key] = copy.deepcopy(value)
    if feed_upserts:
        merged["feed_upserts"] = list(feed_upserts.values())
    return {"mode": "delta", "revision": revision, "changes": merged}


def _queue_item_locked(item):
    proposed = [
        queued for queued in STATE["queue"]
        if not (item.get("kind") == "control"
                and queued.get("kind") == "control"
                and queued.get("selector") == item.get("selector"))
    ]
    _require_quota("queue_items", len(proposed) + 1, MAX_QUEUE_ITEMS)
    item["qid"] = secrets.token_hex(4)
    item["ts"] = time.time()
    if item["kind"] == "control":
        STATE["queue"] = [
            queued for queued in STATE["queue"]
            if not (queued["kind"] == "control"
                    and queued.get("selector") == item.get("selector"))
        ]
    STATE["queue"].append(item)
    return item


def _require_quota(resource, proposed, limit):
    if proposed > limit:
        raise QuotaExceeded(resource, limit, proposed)


def _append_feed_locked(item):
    STATE["feed"].append(item)
    if len(STATE["feed"]) > MAX_FEED_ITEMS:
        del STATE["feed"][:-MAX_FEED_ITEMS]


def _warning_key(finding):
    return "|".join(str(finding.get(field)) for field in
                    ("selector", "kind", "axis", "severity"))


def _undelivered_warnings_locked():
    """Severe findings this session has not already sent to the agent.

    Minor findings are the browser's business. Repeating either kind on every
    feedback batch made the agent re-read warnings it had already handled.
    """
    fresh = []
    for finding in _severe(STATE["audit"]["findings"]):
        key = _warning_key(finding)
        if key in STATE["warned"]:
            continue
        STATE["warned"].append(key)
        fresh.append(finding)
    return fresh


def _applied_event_locked(items):
    """Tell the agent which of its own words the reviewer already rewrote.

    This is a feedback event like any other, so the agent's existing loop
    reads it. ``applied`` says the artifact already carries these changes, so
    the agent confirms and moves on instead of making them a second time.
    """
    _require_quota(
        "pending_events", len(STATE["events"]) + 1, MAX_PENDING_EVENTS)
    sent_at = time.time()
    event = event_envelope(
        "feedback",
        id=secrets.token_hex(8),
        items=items,
        applied=True,
        layout_warnings=_undelivered_warnings_locked(),
        sent_at=sent_at,
    )
    _append_feed_locked({
        "id": event["id"],
        "role": "human",
        "ts": sent_at,
        "items": items,
        "status": "sent",
        "applied": True,
    })
    STATE["events"].append(event)
    return event


def _feedback_event_locked():
    if not STATE["queue"]:
        return None
    _require_quota(
        "pending_events", len(STATE["events"]) + 1, MAX_PENDING_EVENTS)
    items = STATE["queue"]
    STATE["queue"] = []
    sent_at = time.time()
    event = event_envelope(
        "feedback",
        id=secrets.token_hex(8),
        items=items,
        layout_warnings=_undelivered_warnings_locked(),
        sent_at=sent_at,
    )
    _append_feed_locked({
        "id": event["id"],
        "role": "human",
        "ts": sent_at,
        "items": items,
        "status": "sent",
    })
    STATE["events"].append(event)
    return event


def _drop_emptied_block(source, index):
    """Remove the tag pair a cut just emptied, at ``index`` in ``source``.

    A reviewer who cuts a whole line means the line goes, not that an empty
    paragraph stays behind. Only the tag the cut text sat directly inside is
    considered, and only when nothing but whitespace is left in it.
    """
    opening = None
    for match in re.finditer(r"<([a-zA-Z][\w-]*)\b[^>]*>", source[:index]):
        opening = match
    if not opening:
        return source
    closing = re.compile(
        rf"</{re.escape(opening.group(1))}\s*>", re.I).search(source, index)
    if not closing:
        return source
    if source[opening.end():closing.start()].strip():
        return source
    head = source[:opening.start()]
    tail = source[closing.end():]
    # The line the tag stood on goes with it rather than staying as blank space.
    if tail[:1] in ("\n", "\r"):
        head = head.rstrip(" \t")
        if head.endswith(("\n", "\r")):
            tail = tail[2:] if tail[:2] == "\r\n" else tail[1:]
    return head + tail


def _apply_block_edit(source, before, after):
    """Replace ``before`` with ``after`` once, or say why it cannot be done.

    Returns ``(source, None)`` on success and ``(source, reason)`` otherwise.
    A block whose text the file does not carry exactly once is left alone: the
    reviewer sends that edit to the agent instead of guessing at the source.
    """
    attempts = (
        (before, after),
        (html.escape(before, quote=False), html.escape(after, quote=False)),
    )
    for needle, replacement in attempts:
        count = source.count(needle)
        if count == 1:
            index = source.index(needle)
            rewritten = source[:index] + replacement + source[index + len(needle):]
            if not replacement.strip():
                rewritten = _drop_emptied_block(rewritten, index)
            return rewritten, None
        if count > 1:
            return source, "the same text appears more than once in the file"
    return source, "the file does not carry this text as one plain run"


def _stale_edit(item, version):
    """Say whether the artifact moved after this edit was drawn on it.

    The version is a nanosecond mtime, which is wider than the integers
    JavaScript holds exactly, so the browser stamps a rounded copy on the edit.
    Rounding both sides the same way compares what the browser actually saw.
    """
    base = item.get("baseVersion")
    return base is not None and float(base) != float(version)


def _apply_text_edits(items, version):
    """Write queued text edits into the artifact, in the order they were made.

    Returns ``(applied_items, refusals)``. Applying stops at nothing: an edit
    the source cannot carry is refused on its own and the rest still land.
    """
    with open(ARTIFACT, encoding="utf-8") as handle:
        source = handle.read()
    applied = []
    refusals = []
    for item in items:
        if _stale_edit(item, version):
            refusals.append({
                "qid": item.get("qid"),
                "reason": "the artifact changed after this edit was drawn on it",
            })
            continue
        working = source
        reason = None
        for block in item.get("blocks") or []:
            working, reason = _apply_block_edit(
                working, block["before"], block.get("after", ""))
            if reason:
                break
        if reason:
            refusals.append({"qid": item.get("qid"), "reason": reason})
            continue
        source = working
        applied.append(item)
    if applied:
        _write_artifact(source)
    return applied, refusals


def _write_artifact(source):
    """Replace the artifact atomically, keeping the file's own permissions."""
    tmp = ARTIFACT + ".arev-tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.write(source)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(tmp, os.stat(ARTIFACT).st_mode & 0o777)
    except OSError:
        pass
    os.replace(tmp, ARTIFACT)


def _watch_file():
    """Publish artifact changes and invalidate layout results from older bytes."""
    last = None
    while True:
        try:
            mtime = os.stat(ARTIFACT).st_mtime_ns
        except OSError:
            mtime = last
        if mtime != last:
            with STATE_LOCK:
                STATE["version"] = mtime or 0
                if last is not None:
                    # Content changed: previous audit no longer describes it.
                    STATE["audit"] = {"status": "pending", "findings": []}
                    # A warning that survives an edit was not fixed, so let the
                    # re-audit report it to the agent again.
                    STATE["warned"] = []
                    STATE["events"] = [
                        event for event in STATE["events"]
                        if event.get("type") != "layout"
                    ]
                    _persist_locked()
                _changed_locked()
            last = mtime
        time.sleep(0.5)


def _severe(findings):
    return [
        finding for finding in findings
        if isinstance(finding, dict) and finding.get("severity") == "severe"
    ]


def _write_private_json(path, value):
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(value, fh, indent=2)
            fh.flush()
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _utc_timestamp():
    return datetime.now(timezone.utc).isoformat(
        timespec="milliseconds").replace("+00:00", "Z")


def _validated_whiteboard_id(value):
    if isinstance(value, str) and WHITEBOARD_ID_RE.fullmatch(value):
        return value
    return None


def _validated_source_hash(value):
    if isinstance(value, str) and SOURCE_HASH_RE.fullmatch(value):
        return value
    return None


def _normalise_text_edit(item):
    """Check a text edit carries block text the artifact file can be searched for.

    ``blocks`` is what a save writes: one before-and-after pair per block the
    reviewer changed. ``before``/``after`` are the shorter pair the review rail
    shows, so they are read but never used to rewrite the file.
    """
    blocks = item.get("blocks")
    if not isinstance(blocks, list) or not blocks:
        raise ValueError("text-edit needs at least one block")
    if len(blocks) > MAX_TEXT_EDIT_BLOCKS:
        raise ValueError(
            f"text-edit carries more than {MAX_TEXT_EDIT_BLOCKS} blocks")
    if item.get("action") not in ("edit", "cut"):
        raise ValueError("text-edit action must be edit or cut")
    total = 0
    for block in blocks:
        if not isinstance(block, dict):
            raise ValueError("text-edit block is not an object")
        before = block.get("before")
        after = block.get("after", "")
        if not isinstance(before, str) or not before.strip():
            raise ValueError("text-edit block needs the text it replaces")
        if not isinstance(after, str):
            raise ValueError("text-edit block replacement must be a string")
        total += len(before) + len(after)
    if total > MAX_TEXT_EDIT_CHARS:
        raise ValueError(
            f"text-edit carries more than {MAX_TEXT_EDIT_CHARS} characters")
    return item


def _normalise_feedback_item(item):
    if item.get("kind") == "text-edit":
        return _normalise_text_edit(item)
    target = item.get("target")
    if (item.get("kind") != "element"
            or not isinstance(target, dict)
            or target.get("type") != "mermaid-node"):
        return item
    normalised = {"type": "mermaid-node"}
    for field, limit in MERMAID_NODE_TARGET_LIMITS.items():
        value = target.get(field)
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(
                f"mermaid-node target {field} must be a string "
                f"of at most {limit} characters")
        normalised[field] = value
    item["target"] = normalised
    return item


def _whiteboard_dir_locked():
    path = os.path.join(SESSION_DIR, "whiteboards")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _working_whiteboard_path(whiteboard_id):
    return os.path.join(
        SESSION_DIR, "whiteboards", whiteboard_id + ".working.json")


def _discard_working_whiteboards_locked():
    """Drop autosaved editor drafts that were never sent, once a review ends.

    A draft only exists so a reload or a crash does not lose the scene being
    edited, which is why a write is refused after the session ends. Keeping the
    file past that point makes the next review open the editor on an edit the
    agent never received, with nothing on screen to say so.
    """
    # ARTIFACT_REVIEW_HOME sets where the session directory lives, so its path
    # can hold "[" or "?". Escape it before it becomes part of a glob pattern.
    directory = glob.escape(os.path.join(SESSION_DIR, "whiteboards"))
    for path in glob.glob(os.path.join(directory, "*.working.json")):
        try:
            os.unlink(path)
        except OSError:
            pass


def _normalise_working_record(value):
    if not isinstance(value, dict):
        raise ValueError("working whiteboard record is not an object")
    scene = value.get("scene")
    baseline = value.get("baseline")
    source_hash = _validated_source_hash(value.get("source_hash"))
    metrics_version = value.get("text_metrics_version")
    updated_at = value.get("updated_at")
    if not isinstance(scene, dict):
        raise ValueError("working whiteboard scene is not an object")
    if baseline is not None and not isinstance(baseline, dict):
        raise ValueError("working whiteboard baseline is malformed")
    if source_hash is None:
        raise ValueError("working whiteboard source hash is malformed")
    if (not isinstance(metrics_version, int)
            or isinstance(metrics_version, bool)):
        raise ValueError("working whiteboard text metrics version is malformed")
    if not isinstance(updated_at, str) or len(updated_at) > 64:
        raise ValueError("working whiteboard timestamp is malformed")
    return {
        "source_hash": source_hash,
        "text_metrics_version": metrics_version,
        "updated_at": updated_at,
        "scene": scene,
        "baseline": baseline,
    }


def _whiteboard_blob_dir_locked():
    path = os.path.join(_whiteboard_dir_locked(), "blobs")
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _digest_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def _write_blob_atomic(path, value, digest):
    if os.path.exists(path):
        if os.path.getsize(path) != len(value) or _digest_file(path) != digest:
            raise OSError(f"content-addressed blob is corrupt: {path}")
        return False
    tmp = os.path.join(
        os.path.dirname(path),
        f".{os.path.basename(path)}.{secrets.token_hex(8)}.tmp",
    )
    try:
        with open(tmp, "xb") as handle:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, path)
        return True
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _save_whiteboard_blobs_locked(scene, png_bytes):
    scene_bytes = json.dumps(
        scene, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    scene_hash = hashlib.sha256(scene_bytes).hexdigest()
    png_hash = (
        hashlib.sha256(png_bytes).hexdigest()
        if png_bytes is not None else None
    )
    directory = _whiteboard_blob_dir_locked()
    targets = [(
        os.path.join(directory, scene_hash + ".excalidraw"),
        scene_bytes,
        scene_hash,
    )]
    if png_bytes is not None:
        targets.append((
            os.path.join(directory, png_hash + ".png"),
            png_bytes,
            png_hash,
        ))

    added = []
    added_bytes = 0
    for path, value, digest in targets:
        if os.path.exists(path):
            if os.path.getsize(path) != len(value) or _digest_file(path) != digest:
                raise OSError(f"content-addressed blob is corrupt: {path}")
        else:
            added.append((path, value, digest))
            added_bytes += len(value)
    usage = STORE.usage()
    _require_quota(
        "snapshot_blobs",
        usage["snapshot_blobs"] + len(added),
        MAX_SNAPSHOT_BLOBS,
    )
    _require_quota(
        "snapshot_bytes",
        usage["snapshot_bytes"] + added_bytes,
        MAX_SNAPSHOT_BYTES,
    )

    created = []
    try:
        for path, value, digest in added:
            if _write_blob_atomic(path, value, digest):
                created.append(path)
    except Exception:
        for path in created:
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
        raise
    return {
        "scene_path": targets[0][0],
        "png_path": targets[1][0] if len(targets) > 1 else None,
        "scene_hash": scene_hash,
        "png_hash": png_hash,
    }


def _normalise_host(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return urlparse("//" + value).hostname
    except ValueError:
        return None


class ReviewHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # -- guards ------------------------------------------------------------
    def _guard(self):
        raw_host = (self.headers.get("Host") or "").strip()
        try:
            host = urlparse("//" + raw_host).hostname or ""
        except ValueError:
            host = ""
        if host.lower() not in ALLOWED_HOSTS:
            self._json({"error": "bad host"}, 403)
            return False
        path = urlparse(self.path).path
        if self.command == "GET" and path in PUBLIC_REVIEW_PATHS:
            return True
        qs = parse_qs(urlparse(self.path).query)
        token = self.headers.get("X-Arev-Token") or (qs.get("t") or [""])[0]
        if not isinstance(token, str) or not secrets.compare_digest(token, TOKEN):
            self._json({"error": "bad token"}, 403)
            return False
        return True

    # -- helpers -----------------------------------------------------------
    def _json(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _quota(self, error):
        self._json(error.payload(), 413)

    def _bytes(self, body, ctype, public_static=False):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if public_static:
            # The editor iframe has an opaque sandbox origin, so its ES module
            # and stylesheet loads require CORS despite using the same URL.
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _accepts_gzip(self):
        for item in (self.headers.get("Accept-Encoding") or "").split(","):
            parts = [part.strip() for part in item.split(";")]
            if not parts or parts[0].lower() not in ("gzip", "*"):
                continue
            quality = 1.0
            for parameter in parts[1:]:
                if parameter.lower().startswith("q="):
                    try:
                        quality = float(parameter[2:])
                    except ValueError:
                        quality = 0.0
            if quality > 0:
                return True
        return False

    def _static_entry(self, entry, public_static=False):
        use_gzip = self._accepts_gzip()
        if use_gzip:
            compressed, compressed_etag = _gzip_variant(entry)
            use_gzip = len(compressed) < len(entry["body"])
        body = compressed if use_gzip else entry["body"]
        etag = compressed_etag if use_gzip else entry["etag"]
        version = (parse_qs(urlparse(self.path).query).get("v") or [""])[0]
        cache_control = (
            "public, max-age=31536000, immutable"
            if version == entry["hash"]
            else "public, max-age=0, must-revalidate"
        )

        not_modified = self.headers.get("If-None-Match") == etag
        self.send_response(304 if not_modified else 200)
        self.send_header("Cache-Control", cache_control)
        self.send_header("ETag", etag)
        self.send_header("Vary", "Accept-Encoding")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        if use_gzip:
            self.send_header("Content-Encoding", "gzip")
        if public_static:
            self.send_header("Access-Control-Allow-Origin", "*")
        if not not_modified:
            self.send_header("Content-Type", entry["content_type"])
            self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if not not_modified:
            self.wfile.write(body)

    def _asset(self, name, public_static=False):
        entry = ASSET_CACHE.get(name)
        if entry is None:
            self._json({"error": "asset not found", "asset": name}, 404)
            return
        self._static_entry(entry, public_static=public_static)

    def _body(self):
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length or 0)
        except (TypeError, ValueError):
            self._json({"error": "invalid Content-Length"}, 400)
            return None
        if length < 0:
            self._json({"error": "invalid Content-Length"}, 400)
            return None
        if length > MAX_REQUEST_BYTES:
            self._json({
                "error": "request body too large",
                "max_bytes": MAX_REQUEST_BYTES,
            }, 413)
            return None
        if not length:
            return {}
        try:
            body = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError, RecursionError):
            self._json({"error": "invalid JSON body"}, 400)
            return None
        if not isinstance(body, dict):
            self._json({"error": "JSON body must be an object"}, 400)
            return None
        return body

    # -- GET ---------------------------------------------------------------
    def do_GET(self):
        global LAST_PAGE_POLL
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            self._chrome()
        elif path == "/artifact":
            self._artifact()
        elif path == "/sdk.js":
            self._sdk()
        elif path == "/whiteboard-frame":
            self._asset("whiteboard-frame", public_static=True)
        elif path in ("/whiteboard.js", "/whiteboard.css", "/mermaid.js"):
            self._asset(path.lstrip("/"), public_static=True)
        elif path == "/favicon.ico":
            self._asset("favicon.svg")
        elif path.startswith("/whiteboard/"):
            self._whiteboard_working_get(path[len("/whiteboard/"):])
        elif path == "/state":
            LAST_PAGE_POLL = time.time()
            with STATE_LOCK:
                state = _state_locked()
            self._json(state)
        elif path == "/health":
            self._json({
                "ok": True,
                "instance_id": INSTANCE_ID,
                "tool_version": VERSION,
                "event_schema": EVENT_SCHEMA,
            })
        elif path == "/state/next":
            LAST_PAGE_POLL = time.time()
            self._state_next(parse_qs(urlparse(self.path).query))
        elif path == "/activity":
            self._activity(parse_qs(urlparse(self.path).query))
        elif path == "/next":
            self._next(parse_qs(urlparse(self.path).query))
        else:
            self._json({"error": "not found", "path": path}, 404)

    def _whiteboard_working_get(self, raw_id):
        whiteboard_id = _validated_whiteboard_id(raw_id)
        if whiteboard_id is None:
            self._json({"error": "bad whiteboard id"}, 400)
            return
        path = _working_whiteboard_path(whiteboard_id)
        with STATE_LOCK:
            try:
                with open(path, encoding="utf-8") as fh:
                    saved = _normalise_working_record(json.load(fh))
            except FileNotFoundError:
                saved = None
            except (OSError, ValueError, TypeError, json.JSONDecodeError,
                    RecursionError) as error:
                self._json({
                    "error": f"cannot load working whiteboard: {error}"
                }, 500)
                return
        self._json({"saved": saved})

    def _chrome(self):
        html = ASSET_CACHE["chrome.html"]["body"].decode("utf-8")
        boot = {"token": TOKEN, "artifact": ARTIFACT,
                "name": os.path.basename(ARTIFACT),
                "assets": ASSET_HASHES}
        html = html.replace("/*__AREV_BOOT__*/", "window.AREV = " + json.dumps(boot) + ";")
        self._bytes(html.encode(), "text/html; charset=utf-8")

    def _artifact(self):
        try:
            raw = open(ARTIFACT, encoding="utf-8", errors="replace").read()
        except OSError as err:
            self._bytes(f"<h1>cannot read artifact</h1><p>{err}</p>".encode(), "text/html")
            return
        tag = f'<script src="{_asset_url("sdk.js")}"></script>'
        # Inject before </body> when present, else append. The disk file is
        # untouched, so the artifact opened directly stays identical.
        if re.search(r"</body>", raw, re.I):
            raw = re.sub(r"</body>", tag + "</body>", raw, count=1, flags=re.I)
        else:
            raw += tag
        self._bytes(raw.encode(), "text/html; charset=utf-8")

    def _sdk(self):
        self._asset("sdk.js")

    def _next(self, qs):
        try:
            timeout = float((qs.get("timeout") or ["90"])[0])
        except (TypeError, ValueError):
            timeout = 90.0
        timeout = max(1.0, min(timeout, 600.0))
        deadline = time.time() + timeout
        response = None
        with EVENTS_COND:
            while True:
                now = time.time()
                event = next((
                    candidate for candidate in STATE["events"]
                    if not candidate.get("claimed_at")
                    or candidate["claimed_at"] < now - 30
                ), None)
                if event:
                    event["claimed_at"] = now
                    _persist_locked()
                    response = {
                        key: value for key, value in event.items()
                        if key != "claimed_at"
                    }
                    # Sessions created before public event schemas were added
                    # remain consumable after an in-place tool upgrade.
                    response.setdefault("schema", EVENT_SCHEMA)
                    break
                remaining = deadline - time.time()
                if remaining <= 0:
                    response = event_envelope("idle")
                    break
                EVENTS_COND.wait(timeout=min(remaining, 30.0))
        self._json(response)

    def _state_next(self, qs):
        try:
            after = int((qs.get("after") or ["-1"])[0])
            timeout = float((qs.get("timeout") or ["25"])[0])
        except (TypeError, ValueError):
            after, timeout = -1, 25.0
        timeout = max(1.0, min(timeout, 60.0))
        deadline = time.time() + timeout
        with EVENTS_COND:
            while STATE["revision"] <= after:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                EVENTS_COND.wait(timeout=remaining)
            if (qs.get("mode") or [""])[0] == "delta":
                response = _delta_since_locked(after)
            else:
                response = _state_locked()
        self._json(response)

    def _activity(self, qs):
        try:
            raw_before = (qs.get("before") or [None])[0]
            before = None if raw_before in (None, "") else int(raw_before)
            limit = int((qs.get("limit") or ["50"])[0])
        except (TypeError, ValueError):
            self._json({"error": "before and limit must be integers"}, 400)
            return
        if limit < 1 or limit > 50:
            self._json({"error": "limit must be between 1 and 50"}, 400)
            return
        with STATE_LOCK:
            page = STORE.activity(before=before, limit=limit)
        self._json(page)

    # -- PUT ---------------------------------------------------------------
    def do_PUT(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if not path.startswith("/whiteboard/"):
            self._json({"error": "not found", "path": path}, 404)
            return
        body = self._body()
        if body is None:
            return
        self._whiteboard_working_put(path[len("/whiteboard/"):], body)

    def _whiteboard_working_put(self, raw_id, body):
        whiteboard_id = _validated_whiteboard_id(raw_id)
        if whiteboard_id is None:
            self._json({"error": "bad whiteboard id"}, 400)
            return
        scene = body.get("scene")
        baseline = body.get("baseline")
        source_hash = _validated_source_hash(body.get("source_hash"))
        metrics_version = body.get("text_metrics_version")
        if not isinstance(scene, dict):
            self._json({
                "error": "whiteboard scene must be an object"
            }, 400)
            return
        if baseline is not None and not isinstance(baseline, dict):
            self._json({
                "error": "whiteboard baseline must be an object or null"
            }, 400)
            return
        if source_hash is None:
            self._json({
                "error": "source_hash must be 16-64 lowercase hex characters"
            }, 400)
            return
        if (not isinstance(metrics_version, int)
                or isinstance(metrics_version, bool)):
            self._json({
                "error": "text_metrics_version must be an integer"
            }, 400)
            return
        updated_at = _utc_timestamp()
        record = {
            "source_hash": source_hash,
            "text_metrics_version": metrics_version,
            "updated_at": updated_at,
            "scene": scene,
            "baseline": baseline,
        }
        with EVENTS_COND:
            # The shared state lock orders autosaves with /end and with every
            # other filesystem write made by this session process.
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            try:
                _whiteboard_dir_locked()
                _write_private_json(
                    _working_whiteboard_path(whiteboard_id), record)
            except OSError as error:
                self._json({
                    "error": f"cannot save working whiteboard: {error}"
                }, 500)
                return
        self._json({"ok": True, "updated_at": updated_at})

    # -- POST --------------------------------------------------------------
    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        body = self._body()
        if body is None:
            return
        handler = {
            "/queue": self._queue, "/unqueue": self._unqueue, "/flush": self._flush,
            "/send": self._send, "/agent-reply": self._agent_reply,
            "/agent-status": self._agent_status, "/ack": self._ack, "/end": self._end,
            "/agent-session": self._agent_session,
            "/resume-agent": self._resume_agent,
            "/reopen": self._reopen, "/shutdown": self._shutdown,
            "/audit": self._audit, "/audit/override": self._audit_override,
            "/whiteboard": self._whiteboard,
            "/apply-edits": self._apply_edits,
        }.get(path)
        if handler:
            handler(body)
        else:
            self._json({"error": "not found", "path": path}, 404)

    def _queue(self, body):
        item = body.get("item") or {}
        if not isinstance(item, dict):
            self._json({"error": "item must be an object"}, 400)
            return
        if item.get("kind") not in FEEDBACK_ITEM_KINDS:
            self._json({"error": "bad item kind"}, 400)
            return
        try:
            _normalise_feedback_item(item)
        except ValueError as error:
            self._json({"error": str(error)}, 400)
            return
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            try:
                _queue_item_locked(item)
            except QuotaExceeded as error:
                self._quota(error)
                return
            _persist_locked()
            _changed_locked()
        self._json({"ok": True, "qid": item["qid"], "queued": len(STATE["queue"])})

    def _apply_edits(self, body):
        """Write the queued text edits into the artifact the agent owns.

        The agent is the file's author, so a save is refused outright when the
        file changed after the reviewer drew the edits. What is written is then
        delivered as a feedback event marked ``applied``, because the agent has
        to know its own file moved and who moved it.
        """
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            edits = [item for item in STATE["queue"]
                     if item.get("kind") == "text-edit"]
            if not edits:
                self._json({"error": "no text edits drafted"}, 400)
                return
            try:
                applied, refused = _apply_text_edits(edits, STATE["version"])
            except OSError as error:
                self._json({"error": f"the artifact could not be written: {error}"},
                           500)
                return
            if not applied:
                self._json({"error": refused[0]["reason"], "refused": refused}, 409)
                return
            kept = {id(item) for item in applied}
            STATE["queue"] = [item for item in STATE["queue"]
                              if id(item) not in kept]
            try:
                event = _applied_event_locked(applied)
            except QuotaExceeded as error:
                self._quota(error)
                return
            _persist_locked()
            _changed_locked()
        self._json({"ok": True, "applied": len(applied), "refused": refused,
                    "id": event["id"]})

    def _unqueue(self, body):
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            before = len(STATE["queue"])
            STATE["queue"] = [q for q in STATE["queue"] if q["qid"] != body.get("qid")]
            _persist_locked()
            if before != len(STATE["queue"]):
                _changed_locked()
            self._json({"ok": True, "removed": before - len(STATE["queue"])})

    def _flush(self, body):
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            try:
                event = _feedback_event_locked()
            except QuotaExceeded as error:
                self._quota(error)
                return
            if not event:
                self._json({"error": "queue empty"}, 400)
                return
            _persist_locked()
            _changed_locked()
        self._json({"ok": True, "id": event["id"],
                    "sent": len(event["items"]), "sent_at": event["sent_at"]})

    def _send(self, body):
        item = body.get("item")
        if item is not None:
            if not isinstance(item, dict):
                self._json({"error": "item must be an object"}, 400)
                return
            if item.get("kind") not in FEEDBACK_ITEM_KINDS:
                self._json({"error": "bad item kind"}, 400)
                return
            try:
                _normalise_feedback_item(item)
            except ValueError as error:
                self._json({"error": str(error)}, 400)
                return
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            try:
                # Check the event boundary before adding a supplied item so a
                # rejected send never changes the existing draft queue.
                _require_quota(
                    "pending_events", len(STATE["events"]) + 1,
                    MAX_PENDING_EVENTS)
                if item:
                    _queue_item_locked(item)
                event = _feedback_event_locked()
            except QuotaExceeded as error:
                self._quota(error)
                return
            if not event:
                self._json({"error": "queue empty"}, 400)
                return
            _persist_locked()
            _changed_locked()
        self._json({"ok": True, "id": event["id"],
                    "sent": len(event["items"]), "sent_at": event["sent_at"]})

    def _agent_reply(self, body):
        text = (body.get("text") or "").strip()
        if not text:
            self._json({"error": "empty reply"}, 400)
            return
        reply_to = body.get("reply_to")
        with EVENTS_COND:
            if reply_to:
                for entry in STATE["feed"]:
                    if entry.get("id") == reply_to:
                        entry["status"] = "answered"
                        entry["answered_at"] = time.time()
                        break
            _append_feed_locked({
                "id": secrets.token_hex(8),
                "role": "agent",
                "ts": time.time(),
                "text": text,
                "reply_to": reply_to,
            })
            STATE["agent"] = {"status": "working", "last_seen": time.time()}
            _persist_locked()
            _changed_locked()
        self._json({"ok": True})

    def _agent_status(self, body):
        status = body.get("status")
        if status not in ("offline", "idle", "listening", "working"):
            self._json({"error": "bad agent status"}, 400)
            return
        with EVENTS_COND:
            STATE["agent"] = {"status": status, "last_seen": time.time()}
            _persist_locked()
            _changed_locked()
        self._json({"ok": True})

    def _agent_session(self, body):
        try:
            owner = _normalise_owner(body)
        except ValueError as error:
            self._json({"error": str(error)}, 400)
            return
        with EVENTS_COND:
            STATE["owner"] = owner
            _persist_locked()
            _changed_locked()
        self._json({"ok": True})

    def _resume_agent(self, body):
        global RESUME_LAUNCHED_AT
        with EVENTS_COND:
            owner = copy.deepcopy(STATE["owner"])
            refusal = _resume_refusal_locked(owner)
            if not refusal:
                RESUME_LAUNCHED_AT = time.time()
        if refusal:
            self._json({"error": refusal}, 409)
            return
        command = _resume_command(owner)
        self._json({"ok": True, "launched": _open_terminal(command),
                    "command": command})

    def _ack(self, body):
        event_id = body.get("id")
        status = body.get("status") or "working"
        if not event_id:
            self._json({"error": "missing event id"}, 400)
            return
        if status not in ("idle", "listening", "working"):
            self._json({"error": "bad agent status"}, 400)
            return
        with EVENTS_COND:
            before = len(STATE["events"])
            STATE["events"] = [
                event for event in STATE["events"]
                if event.get("id") != event_id
            ]
            received_at = body.get("received_at") or time.time()
            for entry in STATE["feed"]:
                if entry.get("id") == event_id:
                    entry["status"] = "delivered"
                    entry["delivered_at"] = received_at
                    break
            STATE["agent"] = {"status": status, "last_seen": time.time()}
            _persist_locked()
            _changed_locked()
        self._json({"ok": True, "acknowledged": before - len(STATE["events"])})

    def _end(self, body):
        by = body.get("by") if body.get("by") in ("agent", "user") else "agent"
        with EVENTS_COND:
            if STATE["ended"]:
                if SHUTDOWN_TIMER is None:
                    _schedule_shutdown_locked(self.server)
                existing = next((
                    event for event in STATE["events"]
                    if event.get("type") == "ended"
                ), None)
                event_id = existing.get("id") if existing else None
                self._json({"ok": True, "id": event_id, "already_ended": True})
                return
            STATE["ended"] = True
            STATE["ended_by"] = by
            remaining_events = [
                event for event in STATE["events"]
                if event.get("type") != "layout"
            ]
            try:
                _require_quota(
                    "pending_events", len(remaining_events) + 1,
                    MAX_PENDING_EVENTS)
            except QuotaExceeded as error:
                STATE["ended"] = False
                STATE["ended_by"] = None
                self._quota(error)
                return
            STATE["events"] = remaining_events
            event = event_envelope(
                "ended",
                id=secrets.token_hex(8),
                by=by,
                sent_at=time.time(),
            )
            STATE["events"].append(event)
            _discard_working_whiteboards_locked()
            _persist_locked()
            _changed_locked()
            _schedule_shutdown_locked(self.server)
        self._json({"ok": True, "id": event["id"]})

    def _reopen(self, body):
        with EVENTS_COND:
            _cancel_shutdown_locked()
            STATE["ended"] = False
            STATE["ended_by"] = None
            STATE["events"] = [
                event for event in STATE["events"]
                if event.get("type") not in ("ended", "layout")
            ]
            STATE["audit"] = {"status": "pending", "findings": []}
            STATE["warned"] = []
            _persist_locked()
            _changed_locked()
            state = _state_locked()
        self._json(state)

    def _shutdown(self, body):
        with STATE_LOCK:
            _cancel_shutdown_locked()
        self._json({"ok": True, "instance_id": INSTANCE_ID})
        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _audit(self, body):
        findings = body.get("findings")
        if findings is None:
            findings = []
        if (not isinstance(findings, list)
                or any(not isinstance(finding, dict) for finding in findings)):
            self._json({"error": "findings must be an array of objects"}, 400)
            return
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            severe = _severe(findings)
            remaining_events = [
                event for event in STATE["events"]
                if event.get("type") != "layout"
            ]
            if severe:
                try:
                    _require_quota(
                        "pending_events", len(remaining_events) + 1,
                        MAX_PENDING_EVENTS)
                except QuotaExceeded as error:
                    self._quota(error)
                    return
            STATE["audit"] = {
                "status": "blocked" if severe else "clear",
                "findings": findings,
            }
            # A report supersedes every older layout event. This also removes a
            # stale severe event when a re-audit of changed content is clean.
            STATE["events"] = remaining_events
            if severe:
                # The agent hears about a proven failure immediately. It can
                # fix and re-check before the human ever sees the page.
                STATE["events"].append(event_envelope(
                    "layout",
                    id=secrets.token_hex(8),
                    layout_warnings=findings,
                    sent_at=time.time(),
                ))
            _persist_locked()
            _changed_locked()
        self._json({"ok": True, "status": STATE["audit"]["status"]})

    def _audit_override(self, body):
        with EVENTS_COND:
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            STATE["audit"]["status"] = "overridden"
            _persist_locked()
            _changed_locked()
        self._json({"ok": True})

    def _whiteboard(self, body):
        wid = _validated_whiteboard_id(body.get("id"))
        if wid is None:
            self._json({"error": "bad whiteboard id"}, 400)
            return
        scene = body.get("scene")
        if not isinstance(scene, dict):
            self._json({"error": "whiteboard scene must be an object"}, 400)
            return
        if "source_hash" in body:
            source_hash = _validated_source_hash(body.get("source_hash"))
            if source_hash is None:
                self._json({
                    "error": "source_hash must be 16-64 lowercase hex characters"
                }, 400)
                return
        else:
            source_hash = None
        if ("image_fallback" in body
                and not isinstance(body.get("image_fallback"), bool)):
            self._json({"error": "image_fallback must be a boolean"}, 400)
            return
        if "stats" in body and not isinstance(body.get("stats"), dict):
            self._json({"error": "stats must be an object"}, 400)
            return
        png_bytes = None
        encoded_png = body.get("png_base64")
        if encoded_png is not None:
            if not isinstance(encoded_png, str):
                self._json({"error": "png_base64 must be a string"}, 400)
                return
            try:
                png_bytes = base64.b64decode(encoded_png, validate=True)
            except (binascii.Error, ValueError):
                self._json({"error": "invalid png_base64"}, 400)
                return
            if not png_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                self._json({"error": "decoded whiteboard image is not a PNG"}, 400)
                return
            if len(png_bytes) > MAX_WHITEBOARD_PNG_BYTES:
                self._json({
                    "error": "whiteboard PNG too large",
                    "max_bytes": MAX_WHITEBOARD_PNG_BYTES,
                }, 413)
                return
        with EVENTS_COND:
            # Serialize the final state check with /end so no filesystem write
            # can begin after the session has ended.
            if STATE["ended"]:
                self._json({"error": "session ended"}, 409)
                return
            try:
                saved = _save_whiteboard_blobs_locked(scene, png_bytes)
            except QuotaExceeded as error:
                self._quota(error)
                return
            except OSError as error:
                self._json({"error": f"cannot save whiteboard: {error}"}, 500)
                return
        self._json({
            "ok": True,
            **saved,
            "source_hash": source_hash,
        })


def main():
    global ARTIFACT, SESSION_DIR, STORE, TOKEN, ASSET_DIR, ALLOWED_HOSTS
    global ASSET_CACHE, ASSET_HASHES
    ap = argparse.ArgumentParser(
        description="Internal artifact-review session server. Prefer the "
                    "public `arev open` command.")
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {VERSION}")
    ap.add_argument("--artifact", required=True)
    ap.add_argument("--session-dir", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--asset-dir", required=True)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--allowed-host", action="append", default=[])
    ap.add_argument("--port", type=int, default=0)
    args = ap.parse_args()

    ARTIFACT = os.path.realpath(args.artifact)
    SESSION_DIR = os.path.realpath(args.session_dir)
    TOKEN = args.token
    ASSET_DIR = os.path.realpath(args.asset_dir)
    if not os.path.isfile(ARTIFACT):
        ap.error(f"artifact is not a file: {ARTIFACT}")
    if not os.path.isdir(ASSET_DIR):
        ap.error(f"asset directory does not exist: {ASSET_DIR}")
    try:
        ASSET_CACHE = _load_asset_cache(ASSET_DIR)
    except OSError as error:
        ap.error(f"cannot load review runtime assets: {error}")
    ASSET_HASHES = {
        name: entry["hash"]
        for name, entry in ASSET_CACHE.items()
        if name != "chrome.html"
    }
    if args.port < 0 or args.port > 65535:
        ap.error("--port must be between 0 and 65535")
    try:
        TOKEN.encode("ascii")
    except UnicodeEncodeError:
        ap.error("--token must contain ASCII characters only")
    if len(TOKEN) < 32:
        ap.error("--token must contain at least 32 characters")
    for raw_host in args.allowed_host:
        host = _normalise_host(raw_host)
        if not host:
            ap.error(f"invalid --allowed-host value: {raw_host}")
        ALLOWED_HOSTS.add(host.lower())
    if args.bind not in ("0.0.0.0", "::"):
        ALLOWED_HOSTS.add(args.bind.lower())
    os.makedirs(SESSION_DIR, exist_ok=True)
    try:
        os.chmod(SESSION_DIR, 0o700)
    except OSError:
        pass
    STORE = ReviewStore(SESSION_DIR, STATE_SCHEMA)
    STORE.set_session_info(ARTIFACT)
    _restore()
    with STATE_LOCK:
        _reset_publication_locked()

    threading.Thread(target=_watch_file, daemon=True).start()
    server_class = ReviewHTTPServer
    if ":" in args.bind:
        class IPv6ReviewHTTPServer(ReviewHTTPServer):
            address_family = socket.AF_INET6
        server_class = IPv6ReviewHTTPServer
    server = server_class((args.bind, args.port), Handler)
    print(f"LISTENING {server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with STATE_LOCK:
            _cancel_shutdown_locked()
        server.server_close()
        STORE.close()


if __name__ == "__main__":
    main()
