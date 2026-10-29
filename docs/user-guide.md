# User guide

Artifact Review keeps an agent-authored HTML artifact open in a browser while
you and the agent exchange precise, structured feedback.

## Start a review

Once installed, an agent can reach for the skill when it proposes work such as
a feature design, implementation plan, approach comparison, or other idea that
needs your opinion. Instead of presenting the proposal only in chat, it can
open an HTML artifact that you annotate directly.

You can also invoke the skill by name:

> Use `$artifact-review` to create or open this report for visual review, then
> apply my feedback until I approve it.

The normal loop is:

1. The agent creates or updates an HTML artifact in your project.
2. Artifact Review opens a tokenized local review URL.
3. The agent waits for feedback in a foreground poll.
4. You add review items and select **Send now**.
5. The agent receives the batch, edits the source artifact, and replies.
6. The browser live-reloads the updated artifact.

Multiple queued items are sent as one event so related feedback reaches the
agent together.

## Review an artifact

Review items can include:

- selected text or element annotations;
- native form choices;
- chat messages;
- direct text edits and deletions; and
- diagram whiteboard edits.

Use **Add to review** when you want to keep collecting drafts. Use **Send now**
when the current batch is ready for the agent.

## Edit text directly

**Edit text** mode (`⌘E`) is for changes that are faster to make than to
describe. Point at a line to reveal a pencil for editing and a bin for
deletion. Selecting words, including a selection across several lines, offers
**Edit text**, **Delete**, and **Comment** for that range.

The selected text becomes an in-place editor containing the current words.
Press `⌘Enter` to save the change as a draft, or `Escape` to restore the
original text.

Direct edits appear in the review rail with the other drafts. The composer then
offers:

| Choice | Result |
| --- | --- |
| **Save edits to the artifact** | Rewrites the file immediately and tells the agent what changed |
| **Send now** | Asks the agent to make the changes; the file remains untouched until it does |
| **Add to review** | Keeps everything drafted for a later send |

A direct save is refused—and remains drafted for the agent—if the file changed
after the edit was created, or if the original text is not one unique,
contiguous plain-text match in the file. This prevents a stale or ambiguous
edit from changing the wrong content.

## Review diagrams

Mermaid blocks written as `<pre class="mermaid" id="stable-id">…</pre>` receive
an **Open diagram editor** control. Flowchart, sequence, class, ER, and state
diagrams convert into editable shapes. Other valid Mermaid types use an image
annotation fallback instead of presenting an empty canvas.

Use **Fullscreen** when you need more room. Diagram edits autosave locally after
800 ms without changing the artifact source. The active scene is saved before
you switch diagrams or reload the artifact.

If the Mermaid source changes after an edit, Artifact Review asks you to choose
between **Re-convert (discard saved edits)** and **Keep editing saved scene**.
It does not silently merge or discard your work. Ending the review discards
working scenes that were never submitted.

Rendered Mermaid SVG remains annotatable. Element feedback identifies the
specific diagram node instead of only the surrounding image.

![Inline diagram editing with autosave and fullscreen](images/inline-diagram-review.png)

![ER diagram converted to editable shapes](images/er-diagram-review.png)

## Understand review states

The review rail distinguishes browser drafts, durable delivery, agent pickup,
and agent replies:

| State | Meaning |
| --- | --- |
| Draft | Queued in the browser but not yet sent |
| Sending | The browser request is in flight |
| Sent | The local server durably stored the feedback |
| Received | The agent claimed and acknowledged the feedback |
| Answered | The agent replied after handling the feedback |
| Applied | You saved a direct text edit to the artifact |
| Failed | Sending failed; the draft remains available to retry |

**Applied** is a direct-edit outcome, not a delivery transition. A batch can be
Sent quickly while it waits in Received for an active agent poll, and an agent
may need additional time to reason, use tools, and edit the file before the
batch becomes Answered.

See [Architecture](architecture.md#delivery-and-latency) when measuring these
phases.

## Resume the agent

Sent feedback waits on the server until an agent polls, even after the agent
session that opened the review has closed. When no agent polls, select
**Resume** in the top bar. The review then opens a new terminal window. That
window resumes the agent session that opened the review and tells it to poll
the review.

- **Resume** is dimmed while an agent polls or works, and for 60 seconds after
  a resume starts.
- **Resume** is available only when a Claude Code session opened the review.
  Claude Code gives its session ID to `arev open`. Other agents do not, so the
  button stays dimmed for them.
- When the agent session is still open in another terminal, the review refuses
  to resume it. Go to that terminal and ask the agent to poll.
- The window opens on macOS only. On other platforms, the review copies the
  resume command to the clipboard. Paste it into a terminal on the machine
  that runs the review.

## Reports, archives, and cleanup

Artifact Review stores session history locally. Use the CLI to turn that
history into durable output:

```bash
"$AREV" report "$ARTIFACT"
"$AREV" report "$ARTIFACT" --format markdown -o REVIEW.md
"$AREV" archive "$ARTIFACT" -o REVIEW.zip
```

Reports contain artifact and Git identity, ordered feedback and replies,
delivery timestamps, and snapshot hashes. An archive adds a consistent session
database snapshot and referenced diagram assets without copying the source
artifact.

Preview old ended sessions before removing them:

```bash
"$AREV" prune --older-than 30
"$AREV" prune --older-than 30 --apply
```

`prune` is a dry run unless `--apply` is present. See the [CLI
reference](cli.md) for launcher setup and all commands.

## End a review

Ending a review schedules its local server to stop five minutes later. An open
review tab can keep the server available while it continues polling, for up to
one hour. Reopening the review cancels the stop timer.

Working diagram edits that were never submitted are discarded when the review
ends and do not reappear in the next session.
