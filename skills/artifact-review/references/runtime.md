# Artifact Review runtime

Read this reference only for setup, lifecycle, delivery-state, whiteboard, or
recovery details. The compact workflow stays in `SKILL.md`.

## Installation and authoring

Artifact Review requires Python 3.9+ and a modern browser; its installed
runtime has no third-party Python or Node dependency. If `brief` reports a
failed install check, report the missing component instead of rebuilding the
runtime elsewhere. `doctor` prints the full diagnostic paths and checks.

Run `brief` once per artifact, then `new`. The scaffold already supplies theme,
dark mode, overflow, focus, and reading-width rules. Treat every selected
playbook as a requirement for content inside the marked region. Do not write
review state, generated whiteboard scenes, or screenshots into the skill.

The review server renders every `<pre class="mermaid">` block in the page
offline with its own pinned Mermaid, so an artifact must not load a second
Mermaid under the review server.
Diagram colors, text, and font derive from the page's rendered palette, and
diagrams re-render automatically when the viewer flips the page theme, so
never hardcode a Mermaid theme. Rendered diagrams zoom on a Ctrl or Cmd wheel
and pan on a drag. A plain wheel and a vertical finger drag stay with the page.
A double click restores the original size, and all three gestures freeze in
annotate mode. A diagram with no title of its own gets one naming the gestures,
which the browser shows as a tooltip. Set `accTitle` to keep your own title
instead. Every node gets a stable identity key that survives
re-renders. Flowchart, sequence, class, ER, and state Mermaid diagrams become
editable shapes. Other valid Mermaid types use a labeled image-annotation
fallback.

A page that must also draw its diagrams when opened from disk loads its own
Mermaid only under `file:`, with a dynamic import:

```html
<script type="module">
  if (location.protocol === "file:") {
    const { default: mermaid } = await import(
      "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs");
    mermaid.initialize({ startOnLoad: false });
    await mermaid.run();
  }
</script>
```

Call `mermaid.run()` yourself. `startOnLoad` waits for the window `load` event,
which has already fired when the dynamic import resolves.

Under the review server that branch never runs. `check` fails a `<script src>`
or a static `import` of Mermaid, because both always run and add a second
renderer. `check` warns on a dynamic `import()` of Mermaid as a reminder to keep
the import and every Mermaid call inside the `file:` branch.

The boot layout audit runs at desktop width and again at phone (360px) and
tablet (800px) widths behind the curtain. Findings carry a `viewportClass`
and their evidence names the viewport, so fix narrow-screen failures with
responsive layout, not by resizing your own browser.

## Sessions and polling

Session identity is the artifact's real path. `open` starts or reuses its one
server and live-reloads source saves. `poll` long-waits for one durable event;
an event with an id is acknowledged automatically. The review panel then moves
that delivery from **Sent** to **Received**.

The default `poll --timeout 110` belongs under a tool timeout longer than 110
seconds. For example, use `--timeout 570` only under a 600-second tool timeout.
A killed poll wastes its wait but does not erase a queued event.

In Claude Code, `open` records the calling session, so the reviewer's
**Resume** button can reopen it after the session closes. A resumed session
receives a prompt that names the artifact. Run `open` and `poll` for that file.

Prefer `reply` after the source edit is saved. `reply --to EVENT_ID` targets an
event other than the last received one. `poll --agent-reply` posts before it
waits, so use it only when that ordering is intended.

## Delivery states

- **Draft**: only in the browser queue.
- **Sending**: request in flight.
- **Sent**: local server persisted the event.
- **Received**: the foreground poll claimed and acknowledged it.
- **Answered**: the agent posted a reply for that event.
- **Failed**: sending failed; the browser kept the draft for retry.

`sent_at` measures persistence, `delivered_at - sent_at` measures poll pickup,
and `answered_at - delivered_at` measures agent turnaround. Agent scheduling,
reasoning, tools, and edits happen after Received.

## Whiteboard lifecycle

Diagram hosts are cheap until the reviewer activates one. One shared sandboxed
editor frame moves between diagrams; the active scene flushes before a switch
or artifact reload. Working scenes autosave privately and last only as long as
the review: ending it drops every autosave that was never sent, so the next
review opens each diagram from the artifact's own Mermaid source. If Mermaid
source and a saved scene have different hashes, the browser asks the reviewer
to re-convert or keep the older scene. Never make that choice for them.

Whiteboard feedback contains a `summary`, an optional reviewer `note`,
auto-generated `summary_lines` (one sentence per added, removed, moved,
relabeled, or drawn element, with node labels and arrow endpoints), structured
`stats`, a `scene_path`, and usually a `png_path`. Start with the note and
summary lines, inspect the structured scene if needed, and read the PNG only
when intent is still ambiguous. Update the authoritative Mermaid/HTML; never
replace it with the Excalidraw scene.

## End and reopen

Respect a browser-ended session. Do not reopen it until the concern has been
addressed and a new review is appropriate. Then use:

```bash
"$AREV" open "$ARTIFACT" --reopen
```

Agent completion uses `"$AREV" end "$ARTIFACT"`. Process cleanup uses
`"$AREV" stop "$ARTIFACT"`; use `stop --all` only when stopping every review
server is intentionally in scope. An ended server shuts itself down five minutes
after the last review tab stops polling, so a reviewer never loses the session
mid-edit, and after an hour whatever the tab does. Reopening within that window
cancels the shutdown.

## Durable history, reports, and retention

Session state is normalized in a private SQLite database. Browser state starts
with the newest 50 activity entries, loads older entries in 50-entry pages, and
uses bounded revision deltas after that. Identical diagram snapshots share
content-addressed files. Queue, event, history, snapshot-count, and snapshot-byte
limits prevent one long review from growing without bound.

Create a reusable record without opening the browser:

```bash
"$AREV" report "$ARTIFACT" --format markdown -o review.md
"$AREV" archive "$ARTIFACT" -o review.zip
```

Reports include artifact and Git identity, ordered feedback/replies, delivery
timestamps, and diagram snapshot hashes. Archives include the report, a
consistent database copy, and referenced snapshot blobs—not the source
artifact. End or stop a live review before archiving.

Retention is explicit and safe by default:

```bash
"$AREV" prune --older-than 30       # preview only
"$AREV" prune --older-than 30 --apply
```

Only ended, stopped sessions older than the threshold are candidates. Symlinks
and paths outside the direct sessions directory are refused.

State defaults to `~/.artifact-review` and may contain feedback, replies,
scenes, previews, and logs. Override `ARTIFACT_REVIEW_HOME` only for another
private local directory.
