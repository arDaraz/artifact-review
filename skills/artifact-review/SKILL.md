---
name: artifact-review
description: Use when proposing, planning, comparing, reviewing, annotating, approving, or refining agent-authored HTML artifacts, feature designs, implementation plans, architecture specs, visual reports, diagrams, forms, tables, or slide-like pages; also when the user names Artifact Review or arev. Not for ordinary webpage styling or claude.ai Artifact publishing.
---

# Artifact Review

Artifact Review is the browser loop for proposed work. Build or update one HTML
file, let the user point, choose, draw, or write, then edit that same source
until the review is complete. Direct factual answers and fully specified edits
can stay in conversation.

## Core contract

- Keep the artifact in the user's workspace. The review runtime observes it and
  writes nothing of its own. The one exception is a text edit the reviewer chose
  to save, which arrives as a feedback event marked `applied`. The source HTML
  or Mermaid stays authoritative and stays yours to maintain.
- Resolve `SKILL_ROOT` to this file's directory. On POSIX use
  `AREV="$SKILL_ROOT/scripts/arev"`; on Windows use `scripts\arev.cmd`. Never
  assume `arev` is on `PATH` or hardcode an agent-specific install directory.
- Treat every printed `SESSION` URL as a bearer secret.

## Start one artifact

Run the combined install/design/playbook brief once. Name known playbooks; if
the type is unclear, inspect the index and fetch only matching ids:

```bash
"$AREV" brief plan table
"$AREV" new "/absolute/path/to/review.html" --title "Review title"
```

Fill only the scaffold's `<!-- arev:content -->` region. Every diagram is
Mermaid in `<pre class="mermaid" id="stable-id">` - never hand-built div
boxes, and never a CDN script; the review server renders Mermaid offline and
attaches the editable whiteboard only to those blocks. A page that must also
draw its diagrams when opened from disk uses the guarded fallback in
[runtime.md § Installation and authoring](references/runtime.md#installation-and-authoring).
Diagram colors and fonts derive from the page palette automatically and follow
theme flips, so never set a Mermaid theme or `%%init%%` colors.

Then audit the file before anyone opens it. `check` reads the artifact and the
documents it explains, and fails on a diagram that cannot render and on a
source section the artifact skipped:

```bash
ARTIFACT="/absolute/path/to/review.html"
"$AREV" check "$ARTIFACT"
```

Fix every error and gap, or exclude a section on purpose with
`--ignore "Section title"` and say so in the artifact. Pass `--source PATH`
when the artifact does not name its source in its opening text. Then open the
absolute path once:

```bash
"$AREV" open "$ARTIFACT"
```

One real path owns one live session, so edits reload automatically. If browser
launch is unavailable, give the user the printed private URL to open manually.
Read [runtime.md](references/runtime.md) only when setup, lifecycle, delivery
state, whiteboards, or recovery needs more detail.

## Stay in the foreground loop

```bash
"$AREV" poll "$ARTIFACT"
```

The default 110-second wait fits a 120-second tool limit. Raise the tool limit
before raising `--timeout`. Keep one foreground poll; never background it,
busy-wait, or call `open` repeatedly. Default output is compact JSON; add
`--pretty` only for human inspection.

Route each result, reading only the indicated section:

| Result | Required action |
|---|---|
| `feedback` | Read [events.md § Feedback](references/events.md#feedback), apply the whole batch, save, reply, poll again. |
| `layout` | Read [events.md § Layout](references/events.md#layout), fix every proven severe issue, save, poll again. |
| `ended` | Stop. Read [runtime.md § End and reopen](references/runtime.md#end-and-reopen) before any later reopen. |
| `idle` | Start another foreground poll; queued events are durable. |

A `GUIDANCE STALE` line on stderr means the authoring rules changed after this
session started. Re-run `brief`, apply what moved, and re-run `check` before
continuing.

Re-run `check` after every artifact edit. After an edit is saved, acknowledge
the work in the same session:

```bash
"$AREV" reply "$ARTIFACT" "Applied the requested changes."
```

## Quick reference

| Need | Command |
|---|---|
| Install diagnosis | `"$AREV" doctor` |
| Artifact guidance | `"$AREV" brief [playbook ...]` |
| Scaffold | `"$AREV" new FILE --title TITLE` |
| Audit before opening | `"$AREV" check FILE [--source DOC]` |
| Start/resume | `"$AREV" open FILE` |
| Receive one event | `"$AREV" poll FILE` |
| Reply | `"$AREV" reply FILE TEXT` |
| End lifecycle | `"$AREV" end FILE` |
| Stop process | `"$AREV" stop FILE` |
| Inspect sessions | `"$AREV" sessions` |
| Portable copy | `"$AREV" export FILE -o OUTPUT` |
| Reusable report | `"$AREV" report FILE --format json\|markdown` |
| Review archive | `"$AREV" archive FILE -o REVIEW.zip` |
| Retention preview | `"$AREV" prune --older-than DAYS` |

Before non-loopback use, port forwarding, `--public-url`, or portable export,
read [remote.md](references/remote.md). Do not expose the listener directly to
the public internet.

When the user approves or the review is complete, run `end`. The server stops
five minutes after the last review tab stops polling, and after an hour in any
case. Reopening cancels it. Use `stop` for immediate cleanup.
