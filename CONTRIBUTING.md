# Contributing

Keep runtime dependencies limited to the Python standard library. Changes must retain
the read-only boundary: no model calls, agent control, shell execution of logged text,
or automatic file opening.

Use synthetic fixtures and temporary paths for tests. Never commit real Claude Code transcripts,
user configuration, tokens, screenshots of real sessions, account identifiers, or local project details.
Preserve plain-output support, bounded caches, partial-line handling, and full-detail
escape hatches when changing display filtering. Run tests and Ruff before submitting.

PRs should state the user-visible behavior and relevant functional validation.
