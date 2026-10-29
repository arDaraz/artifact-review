# Security

Artifact Review is a local review tool for agent-authored HTML. Its default
deployment model is one reviewer, one agent, and one machine. It is not a
multi-user web service or a hardened public hosting server.

## Supported versions

Security fixes are made on the latest release and the default branch. Upgrade
to the newest available version before reporting behavior that may already
have been fixed.

## Threat model

The review server:

- binds to `127.0.0.1` by default;
- creates a random token for every artifact session;
- requires that token for the controller, state, and every mutation or
  whiteboard persistence request;
- serves only the reviewed artifact, injected SDK, and inert whiteboard frame
  code without the token so opaque nested frames can load without receiving
  controller credentials;
- validates Host headers on every request;
- renders the artifact in a sandboxed iframe without `allow-same-origin`;
- stores session state locally; and
- does not upload artifacts or feedback to a cloud relay.

These controls protect against accidental cross-origin and network access, but
they are not a replacement for operating-system isolation or a trusted
network.

### Active HTML content

The reviewed artifact is active HTML. Its iframe receives an opaque origin, so
artifact JavaScript cannot read the parent controller, its bearer token,
cookies, or storage through same-origin APIs. The injected SDK exchanges only
structured review messages with the known parent frame.

Inline whiteboards run in nested opaque-origin frames and receive no bearer
token. They can request only typed save or submission operations over a random
per-diagram message channel. The authenticated controller validates that
channel and performs the HTTP request. Public whiteboard routes contain static
code only; state reads and writes remain token-protected.

This browser sandbox is a containment layer, not a promise that hostile HTML is
safe. Artifact code can still render deceptive content, consume resources, and
make network requests allowed by the browser; browser vulnerabilities remain
outside this project's control. Only open artifacts that you generated,
authored, or otherwise trust. Treat untrusted HTML as executable code and
inspect or isolate it before review.

### Session URL

The `SESSION` URL contains a bearer token. Anyone who can reach the listening
port and obtain that URL may access the session. Do not paste the URL into
public logs, issues, screenshots, or chat rooms. Revoke access by stopping the
session.

A token holder can also select **Resume**. On macOS, that starts the recorded
agent session in a new terminal window on the review host. The server builds
that command itself from a validated session ID and directory. The browser
cannot supply command text.

### Local state

The default state directory is `~/.artifact-review`; set
`ARTIFACT_REVIEW_HOME` to use another directory. State can contain:

- artifact paths and session metadata;
- reviewer annotations and agent replies;
- whiteboard scenes and rendered PNGs; and
- local server logs.

The runtime requests restrictive file and directory permissions where the
platform supports them. Your operating-system account and backup policy remain
the security boundary. Do not point `ARTIFACT_REVIEW_HOME` at a shared or
world-readable directory.

### Portable exports

`arev export` creates a single-file copy and may inline local styles, scripts,
images, or other referenced content. Inspect an export before sharing it. It
can contain sensitive information that was not obvious from the visible page.

## Safe remote operation

The runtime does not provide a tunnel, TLS termination, user authentication,
authorization, or a public sharing service.

For a remote development environment:

1. Use an authenticated, access-controlled port forward supplied by the
   development platform.
2. Choose a stable port and the browser-facing `--public-url` before the first
   `open`.
3. Bind only to the interface required by that forward.
4. Keep the tokenized session URL private.
5. Stop the review server when the session is complete.

Avoid binding to `0.0.0.0` on an untrusted network unless the environment
isolates the port and the only route is through the authenticated forward.
Never expose the server directly to the public internet.

If a safe browser route is unavailable, create a portable export and exchange
feedback through an already approved channel instead of weakening network
controls.

## Out of scope

Artifact Review does not claim to:

- make malicious HTML safe or contain browser-engine exploits;
- provide tenant isolation;
- protect a session after its bearer URL is disclosed;
- encrypt files on disk; or
- make an insecure public bind safe.

## Report a vulnerability

Please do not open a public issue for a vulnerability that has not been fixed.
Use a [private GitHub security
advisory](https://github.com/arDaraz/artifact-review/security/advisories/new).

Include, when possible:

- the affected version or commit;
- operating system and browser;
- a minimal reproduction;
- the security impact and required attacker access; and
- any proposed mitigation.

Avoid including real credentials, private artifacts, or live session tokens.
You will receive an acknowledgement after the report is reviewed, followed by
coordination on validation and disclosure as appropriate.
