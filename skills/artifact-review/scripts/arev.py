#!/usr/bin/env python3
"""
arev - review loop for agent-written HTML artifacts.

The CLI starts one standard-library HTTP server per artifact, opens its browser
review surface, and long-polls for annotations, control interactions, chat, and
whiteboard edits. Feedback events are persisted until an agent acknowledges
them. The reviewed file itself is never modified by the tool.

State defaults to ``~/.artifact-review`` and can be relocated with
``ARTIFACT_REVIEW_HOME``. The server binds to loopback unless ``open --bind`` is
used explicitly; a tunnel-facing URL can be supplied with ``--public-url``.
"""

import argparse
import hashlib
import json
import math
import os
import queue
import secrets
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from contextlib import contextmanager

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)
from review_store import ReviewStore
from versioning import EVENT_SCHEMA, STATE_SCHEMA, TOOL_VERSION, event_envelope

VERSION = TOOL_VERSION
MINIMUM_PYTHON = (3, 9)
SKILL_DIR = os.path.dirname(SCRIPT_DIR)
REFERENCE_DIR = os.path.join(SKILL_DIR, "references", "playbooks")
ASSET_DIR = os.path.join(SKILL_DIR, "assets", "review-ui")
# Authoring asset, deliberately outside the directory the server can serve.
TEMPLATE = os.path.join(SKILL_DIR, "assets", "artifact-template.html")
STATE_ROOT = os.path.abspath(os.path.expanduser(
    os.environ.get("ARTIFACT_REVIEW_HOME") or "~/.artifact-review"))
REGISTRY = os.path.join(STATE_ROOT, "registry.json")
REGISTRY_LOCK = os.path.join(STATE_ROOT, "registry.lock")


def _key(path):
    return hashlib.sha1(os.path.realpath(path).encode()).hexdigest()[:12]


def _load_registry_unlocked():
    if os.path.exists(REGISTRY):
        try:
            with open(REGISTRY, encoding="utf-8") as fh:
                value = json.load(fh)
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}
    return {}


def _ensure_private_dir(path):
    os.makedirs(path, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass


def _save_registry_unlocked(reg):
    _ensure_private_dir(STATE_ROOT)
    fd, tmp = tempfile.mkstemp(prefix="registry.", suffix=".tmp", dir=STATE_ROOT)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(reg, fh, indent=2)
            fh.flush()
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
        os.replace(tmp, REGISTRY)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


@contextmanager
def _registry_lock():
    _ensure_private_dir(STATE_ROOT)
    with open(REGISTRY_LOCK, "a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _load_registry():
    with _registry_lock():
        return _load_registry_unlocked()


def _save_registry(reg):
    with _registry_lock():
        _save_registry_unlocked(reg)


def _update_registry(mutator):
    with _registry_lock():
        registry = _load_registry_unlocked()
        result = mutator(registry)
        _save_registry_unlocked(registry)
        return result


def _url_host(bind):
    host = "127.0.0.1" if bind in ("", "0.0.0.0") else "::1" if bind == "::" else bind
    return f"[{host}]" if ":" in host and not host.startswith("[") else host


def _control_url(bind, port):
    return f"http://{_url_host(bind)}:{port}"


def _api(entry, method, path, body=None, timeout=10):
    control_url = entry.get("control_url") or _control_url(
        entry.get("bind", "127.0.0.1"), entry["port"])
    req = urllib.request.Request(
        f"{control_url.rstrip('/')}{path}", method=method,
        headers={"X-Arev-Token": entry["token"], "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return json.loads(res.read())


def _verified_health(entry):
    expected = entry.get("instance_id")
    if not expected:
        return False
    try:
        health = _api(entry, "GET", "/health", timeout=2)
    except (OSError, ValueError, KeyError):
        return False
    return bool(health.get("ok") and health.get("instance_id") == expected)


def _discard_registry_entry(path, expected):
    real = os.path.realpath(path)

    def discard(registry):
        current = registry.get(real)
        if current == expected:
            return registry.pop(real)
        return None

    return _update_registry(discard)


def _entry_for(path, required=True):
    reg = _load_registry()
    real = os.path.realpath(path)
    entry = reg.get(real)
    if entry and _verified_health(entry):
        return entry
    if entry:
        _discard_registry_entry(real, entry)
    if required:
        sys.exit(f"no running session for {path} - run: arev open {path}")
    return None


def _session_url(entry):
    base = (entry.get("base_url") or entry.get("control_url")
            or _control_url(entry.get("bind", "127.0.0.1"), entry["port"]))
    parts = urllib.parse.urlsplit(base)
    query = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if key != "t"
    ]
    query.append(("t", entry["token"]))
    return urllib.parse.urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path or "/",
        urllib.parse.urlencode(query),
        parts.fragment,
    ))


def _open_browser(url):
    try:
        return bool(webbrowser.open(url, new=2))
    except Exception:
        return False


def _update_entry(path, **changes):
    real = os.path.realpath(path)

    def update(registry):
        entry = registry.get(real)
        if entry:
            entry.update(changes)

    _update_registry(update)


def _session_json(path):
    session_dir = os.path.join(STATE_ROOT, "sessions", _key(path))
    database = os.path.join(session_dir, "review.sqlite3")
    legacy = os.path.join(session_dir, "session.json")
    if os.path.exists(database):
        try:
            store = ReviewStore(session_dir, STATE_SCHEMA, read_only=True)
            try:
                return store.load()
            finally:
                store.close()
        except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError):
            return {}
    if os.path.exists(legacy):
        try:
            with open(legacy, encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}
    return {}


def _normalise_host(value):
    value = (value or "").strip()
    if not value:
        return None
    try:
        return urllib.parse.urlsplit("//" + value).hostname
    except ValueError:
        return None


def _public_url(value):
    if not value:
        return None
    try:
        parts = urllib.parse.urlsplit(value)
    except ValueError:
        sys.exit(f"invalid --public-url: {value}")
    if parts.scheme not in ("http", "https") or not parts.hostname:
        sys.exit("--public-url must be an absolute http:// or https:// URL")
    if parts.path not in ("", "/"):
        sys.exit("--public-url cannot include a path prefix yet; use the origin only")
    return value


def _wait_for_listening(proc, timeout=10):
    """Read the server's one-line startup handshake without an unbounded block."""
    lines = queue.Queue(maxsize=1)

    def read_line():
        try:
            lines.put(proc.stdout.readline())
        except Exception:
            lines.put("")

    threading.Thread(target=read_line, daemon=True).start()
    try:
        line = lines.get(timeout=timeout).strip()
    except queue.Empty:
        return None
    if not line.startswith("LISTENING "):
        return None
    try:
        return int(line.split()[1])
    except (IndexError, ValueError):
        return None


def _start_session_server(args, path, public_url):
    token = secrets.token_hex(32)
    session_dir = os.path.join(STATE_ROOT, "sessions", _key(path))
    _ensure_private_dir(session_dir)
    log = open(os.path.join(session_dir, "server.log"), "ab")
    allowed_hosts = []
    for raw_host in args.allow_host:
        host = _normalise_host(raw_host)
        if not host:
            log.close()
            sys.exit(f"invalid --allow-host value: {raw_host}")
        if host.lower() not in allowed_hosts:
            allowed_hosts.append(host.lower())
    if public_url:
        public_host = urllib.parse.urlsplit(public_url).hostname.lower()
        if public_host not in allowed_hosts:
            allowed_hosts.append(public_host)

    popen_options = {}
    if os.name == "nt":
        popen_options["creationflags"] = getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        popen_options["start_new_session"] = True
    command = [
        sys.executable, os.path.join(SCRIPT_DIR, "server.py"),
        "--artifact", path, "--session-dir", session_dir, "--token", token,
        "--asset-dir", ASSET_DIR, "--bind", args.bind, "--port", str(args.port),
    ]
    for host in allowed_hosts:
        command.extend(["--allowed-host", host])
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=log, text=True,
            **popen_options)
    finally:
        log.close()

    port = _wait_for_listening(proc)
    if not port:
        proc.terminate()
        sys.exit(f"server failed to start; see {session_dir}/server.log")

    control_url = _control_url(args.bind, port)
    entry = {
        "port": port,
        "pid": proc.pid,
        "token": token,
        "started": time.time(),
        # What the authoring rules said when this session began. Poll compares
        # against it so a long session notices the rules moved under it.
        "guidance": _guidance_version(),
        "session_dir": session_dir,
        "bind": args.bind,
        "control_url": control_url,
        "base_url": public_url or control_url,
    }
    try:
        health = _api(entry, "GET", "/health", timeout=2)
        instance_id = health.get("instance_id")
        if not health.get("ok") or not instance_id:
            raise ValueError("server returned an invalid health identity")
        entry["instance_id"] = instance_id
    except (OSError, ValueError, KeyError) as error:
        proc.terminate()
        sys.exit(f"server health check failed: {error}")
    return entry


# ----------------------------------------------------------------- commands

def cmd_open(args):
    path = os.path.realpath(args.file)
    if not os.path.isfile(path):
        sys.exit(f"no such file: {path}")

    prior = _session_json(path)
    if prior.get("ended_by") == "user" and not args.reopen:
        sys.exit("the human ended this session from the browser - "
                 "reopen only with --reopen once the concern is addressed")

    public_url = _public_url(args.public_url)
    started = False
    with _registry_lock():
        registry = _load_registry_unlocked()
        entry = registry.get(path)
        if entry and _verified_health(entry):
            if args.reopen:
                try:
                    _api(entry, "POST", "/reopen", {})
                except (OSError, ValueError, KeyError) as error:
                    sys.exit(f"session server could not reopen: {error}")
        else:
            if entry:
                registry.pop(path, None)
                _save_registry_unlocked(registry)
            entry = _start_session_server(args, path, public_url)
            registry[path] = entry
            _save_registry_unlocked(registry)
            started = True

    _register_agent_session(entry)
    url = _session_url(entry)
    print(f"SESSION {url}")
    if (started and args.bind not in ("127.0.0.1", "localhost", "::1")
            and not public_url):
        print("REMOTE NOTE: forward the selected port and pass --public-url on "
              "the next open if the browser is not on this machine.",
              file=sys.stderr)
    if not args.no_browser and not _open_browser(url):
        print("Browser launch was unavailable; open the SESSION URL manually.",
              file=sys.stderr)


def _register_agent_session(entry):
    """Let the review's Resume button reopen the agent session that opened it.

    Only Claude Code exposes its session id to child processes today. Other
    agents leave the button disabled.
    """
    session_id = os.environ.get("CLAUDE_CODE_SESSION_ID")
    if not session_id:
        return
    pid = os.environ.get("CLAUDE_PID", "")
    try:
        _api(entry, "POST", "/agent-session", {
            "agent": "claude-code",
            "session_id": session_id,
            "cwd": os.getcwd(),
            "pid": int(pid) if pid.isdigit() else None,
        })
    except (OSError, ValueError, KeyError) as error:
        print(f"Resume is unavailable for this review: {error}",
              file=sys.stderr)


def _print_event(value, pretty=False):
    if pretty:
        print(json.dumps(value, indent=2))
    else:
        print(json.dumps(value, separators=(",", ":")))


def _guidance_note(entry):
    """Describe how a running session's authoring rules have moved on.

    An agent reads the playbooks once, at the start. Without this it keeps
    writing to rules that were corrected hours ago and never finds out. The
    note goes to stderr rather than into the event, because the event envelope
    is a fixed public shape.
    """
    live = _guidance_version()
    started_with = entry.get("guidance")
    if started_with == live:
        return None
    return (f"GUIDANCE STALE: this session started on rules {started_with or 'unknown'}"
            f" and the installed rules are now {live}. Run: arev brief, apply "
            "what changed, then re-run: arev check")


def cmd_poll(args):
    entry = _entry_for(args.file)
    guidance = _guidance_note(entry)
    if guidance:
        print(guidance, file=sys.stderr)
    try:
        if args.agent_reply:
            _api(entry, "POST", "/agent-reply", {
                "text": args.agent_reply,
                "reply_to": entry.get("last_event_id"),
            })
        _api(entry, "POST", "/agent-status", {"status": "listening"})
        timeout = max(5, args.timeout)
        # One server-side long poll per loop; keep polling until a real event or
        # the caller's timeout budget runs out.
        deadline = time.time() + timeout
        while True:
            remaining = deadline - time.time()
            if remaining <= 0:
                break
            # Round the wait up, never down. Truncating 5.999 to 5 leaves a
            # sub-second remainder that costs another whole server round trip.
            chunk = min(90, math.ceil(remaining))
            event = _api(entry, "GET", f"/next?timeout={chunk}",
                         timeout=chunk + 15)
            _api(entry, "POST", "/agent-status", {"status": "listening"})
            if event.get("type") != "idle":
                if event.get("id"):
                    _api(entry, "POST", "/ack", {
                        "id": event["id"],
                        "status": "working",
                        "received_at": time.time(),
                    })
                    _update_entry(args.file, last_event_id=event["id"])
                _print_event(event, getattr(args, "pretty", False))
                return
        _api(entry, "POST", "/agent-status", {"status": "idle"})
        _print_event(event_envelope("idle"), getattr(args, "pretty", False))
    except (OSError, ValueError, KeyError) as error:
        try:
            _api(entry, "POST", "/agent-status", {"status": "offline"})
        except (OSError, ValueError, KeyError):
            pass
        sys.exit(f"session server unreachable: {error}")


def cmd_end(args):
    entry = _entry_for(args.file)
    _api(entry, "POST", "/end", {"by": "agent"})
    print("ended")


def cmd_reply(args):
    entry = _entry_for(args.file)
    reply_to = args.to or entry.get("last_event_id")
    _api(entry, "POST", "/agent-reply", {
        "text": args.text,
        "reply_to": reply_to,
    })
    print("replied")


def cmd_stop(args):
    if args.all:
        def take_all(registry):
            entries = list(registry.values())
            registry.clear()
            return entries

        entries = _update_registry(take_all)
        stopped = 0
        stale = 0
        for entry in entries:
            if not _verified_health(entry):
                stale += 1
                continue
            try:
                _api(entry, "POST", "/shutdown", {})
                stopped += 1
            except (OSError, ValueError, KeyError):
                stale += 1
        print(f"stopped {stopped} session server(s); removed {stale} stale record(s)")
        return
    if not args.file:
        sys.exit("provide an artifact file or use: arev stop --all")

    def take_entry(registry):
        return registry.pop(os.path.realpath(args.file), None)

    entry = _update_registry(take_entry)
    if not entry:
        print("no running server")
        return
    if not _verified_health(entry):
        print("removed stale session record; no process was signalled")
        return
    try:
        _api(entry, "POST", "/shutdown", {})
    except (OSError, ValueError, KeyError) as error:
        sys.exit(f"verified session could not shut down: {error}")
    print("stopped verified session server")


def cmd_export(args):
    sys.path.insert(0, SCRIPT_DIR)
    from export import export_html
    out = args.output or os.path.splitext(args.file)[0] + ".portable.html"
    result = export_html(args.file, out)
    print(f"wrote {os.path.abspath(out)}  inlined={result['inlined']}"
          + (f"  skipped={result['skipped']}" if result.get("skipped") else ""))


def _session_dir(path):
    return os.path.join(STATE_ROOT, "sessions", _key(path))


def _path_inside(parent, path):
    try:
        return os.path.commonpath((parent, path)) == parent
    except ValueError:
        return False


def _require_review_store(path):
    sessions_root = os.path.realpath(os.path.join(STATE_ROOT, "sessions"))
    unresolved = _session_dir(path)
    session_dir = os.path.realpath(unresolved)
    if (os.path.islink(unresolved)
            or os.path.dirname(session_dir) != sessions_root):
        sys.exit(f"unsafe review session path for {path}")
    database = os.path.join(session_dir, "review.sqlite3")
    if not os.path.isfile(database) or os.path.islink(database):
        sys.exit(f"no durable review state for {path} - run: arev open {path}")
    return session_dir


def cmd_report(args):
    from reports import build_report, render_report, write_report

    path = os.path.realpath(args.file)
    session_dir = _require_review_store(path)
    try:
        report = build_report(path, session_dir)
        if args.output:
            output_path = os.path.realpath(args.output)
            if (output_path == path
                    or _path_inside(session_dir, output_path)):
                sys.exit("report output cannot overwrite artifact or session state")
            output = write_report(report, args.format, args.output)
            print(f"wrote {output}")
        else:
            sys.stdout.write(render_report(report, args.format))
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
        sys.exit(f"could not build review report: {error}")


def cmd_archive(args):
    from reports import write_archive

    path = os.path.realpath(args.file)
    session_dir = _require_review_store(path)
    running = _entry_for(path, required=False)
    try:
        store = ReviewStore(session_dir, STATE_SCHEMA, read_only=True)
        try:
            ended = bool(store.load()["ended"])
        finally:
            store.close()
        if running and not ended:
            sys.exit("end or stop the running review before archiving it")
        output = args.output or os.path.splitext(path)[0] + ".review.zip"
        output_path = os.path.realpath(output)
        if _path_inside(session_dir, output_path):
            sys.exit("archive output cannot overwrite review session state")
        result = write_archive(path, session_dir, output)
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
        sys.exit(f"could not archive review: {error}")
    print(f"wrote {result}")


def _running_session_dirs():
    running = set()
    for path, entry in _load_registry().items():
        if _verified_health(entry):
            running.add(os.path.realpath(
                entry.get("session_dir") or _session_dir(path)))
    return running


def cmd_prune(args):
    from reports import prune_sessions

    try:
        result = prune_sessions(
            STATE_ROOT,
            older_than_days=args.older_than,
            apply=args.apply,
            running_session_dirs=_running_session_dirs(),
        )
    except (OSError, RuntimeError, ValueError, sqlite3.DatabaseError) as error:
        sys.exit(f"could not prune review state: {error}")
    print(json.dumps(result, sort_keys=True, indent=2))


def cmd_check(args):
    """Hold the authored file to the rules before a reviewer ever sees it."""
    from checks import render_json, render_report, run_checks

    path = os.path.abspath(os.path.expanduser(args.file))
    if not os.path.isfile(path):
        sys.exit(f"no such file: {path}")
    try:
        report = run_checks(path, source_paths=args.source, ignore=args.ignore,
                            discover=not args.no_discover)
    except (OSError, ValueError) as error:
        sys.exit(f"could not check {path}: {error}")
    sys.stdout.write(render_json(report) if args.json else render_report(report))
    if not report["ok"] and not args.warn_only:
        sys.exit(1)


def _guidance_version():
    """Fingerprint the authoring rules an agent would have been given.

    Guidance is read once per session. Recording what a session started with
    is the only way a later poll can tell it the rules have since changed.
    """
    sources = [os.path.join(SKILL_DIR, "SKILL.md")]
    sources.extend(
        os.path.join(REFERENCE_DIR, guidance_id + ".md")
        for guidance_id in sorted(_playbook_ids() + ["design"])
    )
    digest = hashlib.sha256()
    for source in sources:
        if not os.path.isfile(source):
            continue
        digest.update(os.path.basename(source).encode())
        with open(source, "rb") as handle:
            digest.update(handle.read())
    return digest.hexdigest()[:12]


def _playbook_ids():
    return sorted(f[:-3] for f in os.listdir(REFERENCE_DIR)
                  if f.endswith(".md") and f != "design.md")


def _use_when(pid):
    """Return a playbook's matcher. It is the first line of the file."""
    with open(os.path.join(REFERENCE_DIR, pid + ".md"), encoding="utf-8") as fh:
        first = fh.readline().strip()
    if first.startswith("use_when:"):
        return first.split(":", 1)[1].strip()
    return ""


def _print_playbook_index():
    for pid in _playbook_ids():
        print(f"{pid}: {_use_when(pid)}")
    print()
    print("usage: arev playbook <id> [<id> ...] - open every playbook whose "
          "use_when matches the artifact")


def _print_playbooks(ids):
    books = _playbook_ids()
    for pid in ids:
        if pid not in books:
            sys.exit(f"unknown playbook '{pid}' (have: {', '.join(books)})")
    for pid in ids:
        with open(os.path.join(REFERENCE_DIR, pid + ".md"), encoding="utf-8") as fh:
            print(fh.read())


def cmd_playbook(args):
    if not args.id:
        _print_playbook_index()
        return
    _print_playbooks(args.id)


def _design_text():
    with open(os.path.join(REFERENCE_DIR, "design.md"), encoding="utf-8") as fh:
        return fh.read()


def cmd_design(args):
    print(_design_text())


def _doctor_checks():
    checks = {
        "python": sys.version.split()[0],
        "skill_dir": SKILL_DIR,
        "state_dir": STATE_ROOT,
        "guidance": _guidance_version(),
        "manifest": os.path.isfile(os.path.join(SKILL_DIR, "manifest.json")),
        "checks": os.path.isfile(os.path.join(SCRIPT_DIR, "checks.py")),
        "store": os.path.isfile(os.path.join(SCRIPT_DIR, "review_store.py")),
        "reports": os.path.isfile(os.path.join(SCRIPT_DIR, "reports.py")),
        "server": os.path.isfile(os.path.join(SCRIPT_DIR, "server.py")),
        "review_ui": os.path.isfile(os.path.join(ASSET_DIR, "chrome.html")),
        "audit": os.path.isfile(os.path.join(ASSET_DIR, "audit.js")),
        "sdk": os.path.isfile(os.path.join(ASSET_DIR, "sdk.js")),
        "template": os.path.isfile(TEMPLATE),
        "offline_whiteboard": all(os.path.isfile(os.path.join(ASSET_DIR, name))
                                  for name in ("whiteboard-frame.html",
                                               "whiteboard.js",
                                               "whiteboard.css")),
        "offline_mermaid": os.path.isfile(
            os.path.join(ASSET_DIR, "mermaid.js")),
        "favicon": os.path.isfile(os.path.join(ASSET_DIR, "favicon.svg")),
    }
    checks["ok"] = all(value for key, value in checks.items()
                       if key not in ("python", "skill_dir", "state_dir",
                                      "guidance"))
    return checks


def cmd_doctor(args):
    checks = _doctor_checks()
    print(json.dumps(checks, indent=2))
    if not checks["ok"]:
        sys.exit(1)


def cmd_new(args):
    """Write the themed artifact shell so no agent has to retype it.

    The shell is the part that is identical in every artifact: theme
    variables, both color schemes, the reading column, overflow containers,
    and control styling. Generating it here costs the caller nothing.
    """
    path = os.path.abspath(os.path.expanduser(args.file))
    if os.path.exists(path) and not args.force:
        sys.exit(f"{path} already exists - pass --force to overwrite it")
    if not os.path.isfile(TEMPLATE):
        sys.exit(f"missing artifact template: {TEMPLATE}")
    title = args.title or os.path.splitext(os.path.basename(path))[0]
    title = title.replace("-", " ").replace("_", " ").strip() or "Artifact"
    with open(TEMPLATE, encoding="utf-8") as fh:
        html = fh.read()
    html = html.replace("__AREV_TITLE__", _escape_html(title))
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)
    print(f"wrote {path}")
    print("fill the region between <!-- arev:content --> and <!-- /arev:content -->")


def _escape_html(value):
    return (value.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def cmd_brief(args):
    """Everything needed before writing an artifact, in one command.

    Separate doctor, design, and playbook calls were four blocking round trips
    whose output is always read together.
    """
    checks = _doctor_checks()
    if not checks["ok"]:
        print("INSTALL failed")
        print(json.dumps(checks, indent=2))
        sys.exit(1)
    print(f"INSTALL ok python={checks['python']}")
    print(f"GUIDANCE {checks['guidance']}")
    print()
    print("## design")
    print(_design_text())
    if args.id:
        print("## playbooks")
        _print_playbooks(args.id)
        return
    print("## playbooks available")
    _print_playbook_index()


def cmd_sessions(args):
    reg = _load_registry()
    if not reg:
        print("no sessions")
        return
    for path, entry in reg.items():
        if _verified_health(entry):
            state = "running"
        else:
            state = "stale record"
            _discard_registry_entry(path, entry)
        saved = _session_json(path)
        if saved.get("ended"):
            state += f", ended by {saved.get('ended_by')}"
        print(f"{state:22} :{entry['port']:<6} {path}")


def main():
    if sys.version_info < MINIMUM_PYTHON:
        required = ".".join(map(str, MINIMUM_PYTHON))
        sys.exit(f"artifact-review requires Python {required} or newer")

    ap = argparse.ArgumentParser(
        prog="arev",
        description="Review agent-written HTML artifacts in a browser and "
                    "exchange durable feedback with a coding agent.",
    )
    ap.add_argument("--version", action="version",
                    version=f"%(prog)s {VERSION}")
    sub = ap.add_subparsers(dest="cmd", metavar="COMMAND")

    p = sub.add_parser(
        "open", help="open or resume an artifact review session",
        description="Start the artifact's review server, or reuse its running "
                    "session, and optionally open it in a browser.")
    p.add_argument("file", help="HTML artifact to review")
    p.add_argument("--reopen", action="store_true",
                   help="reopen a session that the reviewer ended")
    p.add_argument("--no-browser", action="store_true",
                   help="print the session URL without launching a browser")
    p.add_argument("--bind", default="127.0.0.1",
                   help="listen address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=0,
                   help="listen port (default: choose an available port)")
    p.add_argument("--public-url",
                   help="absolute browser-facing http(s) URL from a port forwarder")
    p.add_argument("--allow-host", action="append", default=[],
                   metavar="HOST",
                   help="additional Host header hostname accepted by the server")
    p.set_defaults(fn=cmd_open)

    p = sub.add_parser(
        "poll", help="wait for the next durable review event",
        description="Long-poll until feedback, a layout warning, or an ended "
                    "event is available; acknowledged events are not replayed.")
    p.add_argument("file", help="artifact with a running review session")
    p.add_argument("--timeout", type=int, default=110, metavar="SECONDS",
                   help="overall wait budget (default: 110; minimum: 5). The "
                        "default fits inside a 120s agent tool timeout; raise "
                        "it when the calling agent allows a longer one")
    p.add_argument("--agent-reply",
                   help="post this reply before waiting for the next event")
    p.add_argument("--pretty", action="store_true",
                   help="pretty-print the event JSON instead of one compact line")
    p.set_defaults(fn=cmd_poll)

    p = sub.add_parser("end", help="end a review session as the agent")
    p.add_argument("file"); p.set_defaults(fn=cmd_end)
    p = sub.add_parser("reply", help="post an agent reply without waiting")
    p.add_argument("file"); p.add_argument("text")
    p.add_argument("--to", help="feedback event id (defaults to the last received event)")
    p.set_defaults(fn=cmd_reply)
    p = sub.add_parser("stop", help="stop one or all session servers")
    p.add_argument("file", nargs="?")
    p.add_argument("--all", action="store_true",
                   help="stop every server in the state registry")
    p.set_defaults(fn=cmd_stop)

    p = sub.add_parser("export", help="write a portable single-file artifact")
    p.add_argument("file")
    p.add_argument("-o", "--output"); p.set_defaults(fn=cmd_export)

    p = sub.add_parser(
        "report", help="render the durable review as JSON or Markdown")
    p.add_argument("file", help="reviewed artifact")
    p.add_argument("--format", choices=("json", "markdown"), default="json")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_report)

    p = sub.add_parser(
        "archive", help="write a portable ZIP with review state and snapshots")
    p.add_argument("file", help="reviewed artifact")
    p.add_argument("-o", "--output")
    p.set_defaults(fn=cmd_archive)

    p = sub.add_parser(
        "prune", help="preview or remove old ended review sessions")
    p.add_argument("--older-than", type=float, default=30, metavar="DAYS")
    p.add_argument("--apply", action="store_true",
                   help="remove listed sessions and unreferenced blobs")
    p.set_defaults(fn=cmd_prune)

    p = sub.add_parser(
        "new", help="scaffold a themed, audit-clean artifact shell",
        description="Write a self-contained HTML shell that already satisfies "
                    "the theme, layout, and overflow rules. Fill the marked "
                    "content region instead of writing a page from scratch.")
    p.add_argument("file", help="path to create")
    p.add_argument("--title", help="page title (default: derived from the filename)")
    p.add_argument("--force", action="store_true", help="overwrite an existing file")
    p.set_defaults(fn=cmd_new)

    p = sub.add_parser(
        "check", help="audit an artifact against the rules and its sources",
        description="Read the authored HTML and report diagram problems and "
                    "source sections the artifact left out. Exits non-zero "
                    "when there is an error or a coverage gap.")
    p.add_argument("file", help="HTML artifact to audit")
    p.add_argument("--source", action="append", default=[], metavar="PATH",
                   help="document the artifact explains (repeatable); "
                        "defaults to paths named in the artifact's opening text")
    p.add_argument("--ignore", action="append", default=[], metavar="TEXT",
                   help="source heading to exclude on purpose (repeatable, "
                        "matched as a substring)")
    p.add_argument("--no-discover", action="store_true",
                   help="do not look for source documents automatically")
    p.add_argument("--warn-only", action="store_true",
                   help="report findings but always exit zero")
    p.add_argument("--json", action="store_true",
                   help="emit the full report as JSON")
    p.set_defaults(fn=cmd_check)

    p = sub.add_parser(
        "brief", help="install check, design guidance, and playbooks in one call",
        description="Print everything needed before writing an artifact. Pass "
                    "playbook ids when the artifact type is already known.")
    p.add_argument("id", nargs="*")
    p.set_defaults(fn=cmd_brief)

    p = sub.add_parser("playbook", help="print artifact-type design guidance")
    p.add_argument("id", nargs="*")
    p.set_defaults(fn=cmd_playbook)
    p = sub.add_parser("design", help="print general artifact design guidance")
    p.set_defaults(fn=cmd_design)
    p = sub.add_parser("sessions", help="list known review sessions")
    p.set_defaults(fn=cmd_sessions)
    p = sub.add_parser("doctor", help="check the standalone installation")
    p.set_defaults(fn=cmd_doctor)

    args = ap.parse_args()
    if not args.cmd:
        ap.print_help()
        sys.exit(1)
    args.fn(args)


if __name__ == "__main__":
    main()
