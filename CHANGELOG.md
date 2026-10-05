# Changelog

## 0.1.0 - 2026-10-06

First public release.

- Read-only live feed of local Claude Code sessions and their subagents for a chosen project
  folder, its worktrees, and `.claude/worktrees/` checkouts.
- Focus view: prompts, saved thinking, progress, answers, plans, and agent coordination as
  cards; routine tool calls and results in one line.
- Red errors for failed commands and amber attention markers for denials, hook blocks,
  interruptions, and tool-usage errors, based on the recorded `is_error` flag.
- `--details`, `--full`, `--raw`, `--no-thinking`, `--no-prompts`, `--plain`, and clickable
  local file links in Windows Terminal and other compatible terminals.
- Windows launchers that open Windows Terminal and locate Python 3.11+.
