# Security

Treat local logs as untrusted data. Do not add shell evaluation, executable links
that run automatically, or inline rendering that interprets log control sequences.
The viewer prints sanitized text and generates local file links only on user request
through a mouse click. The only subprocess is `git worktree list`, started from an
absolute path with fsmonitor disabled; keep it that way. Filesystem inspection and Git
require normal user privileges; do not run the viewer as administrator.

Report suspected vulnerabilities privately through
[GitHub private vulnerability reporting](https://github.com/m4d3/ClaudeWatch/security/advisories/new).
Do not post an exploit, real transcripts, or private data in a public issue.

Supported code is the latest published version. Transcript format compatibility is
best effort. No production security certification is claimed.
