# ClaudeWatch

[![Functional checks](https://github.com/m4d3/ClaudeWatch/actions/workflows/ci.yml/badge.svg)](https://github.com/m4d3/ClaudeWatch/actions/workflows/ci.yml)

A lightweight, read-only terminal viewer for local Claude Code activity. Choose a project
folder and follow its main sessions and subagents without resuming or controlling them.
It is the Claude Code counterpart of [CodexWatch](https://github.com/m4d3/CodexWatch).
It is a single Python file that works on Windows, macOS, and Linux.

## Quick start

Requires Python 3.11 or newer. No runtime packages, API keys, or account connection.
Download or clone the repository and run the script directly:

```sh
python claudewatch.py
python claudewatch.py /path/to/project
```

Or install it as a `claudewatch` command with [pipx](https://pipx.pypa.io/):

```sh
pipx install git+https://github.com/m4d3/ClaudeWatch
claudewatch /path/to/project
```

Run it in a second terminal next to Claude Code (CLI, desktop app, or IDE extension).
Without a folder argument it lists your recent Claude Code project folders.

On Windows, double-click `claudewatch.bat` for the folder menu. Both Windows launchers
open Windows Terminal automatically for colors and clickable local file links,
or reuse it if you are already inside it. Administrator mode is not required.
If Windows Terminal is unavailable, the viewer falls back to copyable file paths.
You can paste a folder path if the optional Tk folder picker is unavailable.

```powershell
.\claudewatch.bat "C:\path\to\project"
```

## What it reads

Claude Code saves each session as JSONL under `~/.claude/projects/<project>/`
(or `$CLAUDE_CONFIG_DIR/projects`). The project folder name is the session's starting
path with every non-alphanumeric character replaced by `-`. Subagents are saved next to
their parent as `<session>/subagents/agent-<id>.jsonl`, with a small `.meta.json` file
naming the agent type and task description. ClaudeWatch follows all of these files.

## A feed designed for people

Colors identify activity at a glance: white prompts, cyan thinking and plans, blue progress,
purple agent coordination and file edits, yellow actions, green answers, and red errors.
Routine results stay muted. The Windows launchers explicitly enable colors, including when
`NO_COLOR` is inherited from another app. Use `--plain` or `--color off` to disable them.
Direct Python launches respect `NO_COLOR` unless passed `--color on`.

The default focus view emphasizes your prompts, saved thinking, progress, answers, plans,
and coordination between agents. Routine tool calls and results take one line.

- **Agent coordination** is shown as messages. An `Agent` call becomes a card with the
  subagent type, task description and prompt. A finished subagent's report is shown as an
  agent message, and a background launch takes one line. `SendMessage` and
  `AskUserQuestion` are shown as messages too.
- **Plans** from `TodoWrite`, `ExitPlanMode` and the task tools are rendered as checklists.
- **Background task notifications** become one-line agent activity entries.
- **Errors**: failed commands (`Exit code 1`, tracebacks) get a red ERROR marker.
- **Attention**: permission denials, user rejections, hook blocks, interrupted
  commands, and tool-usage errors such as "file has not been read yet" get an amber
  ATTENTION marker.

```text
  14:12:01  A01  Action: Bash · Run the unit tests
  14:12:03  A01  Result: Bash · 42 lines

  14:12:04  A01  Main session
  Progress
  The build passed. I am checking the preview next.

  14:12:08  A02  ERROR: Bash · exit 1
    Permission denied
```

This example is synthetic. No user logs or screenshots are distributed.

| Option | Purpose |
| --- | --- |
| `--details` | Expanded technical cards, commands, and file references |
| `--full` | Complete text of records within the record-size limit |
| `--raw` | Technical display with tool-call IDs and full session identifiers |
| `--max-lines 60` | Longer human message previews in focus view |
| `--history 0` | Start with new activity only |
| `--history 100` | Request more startup entries, within the history budget |
| `--no-thinking` | Hide saved thinking text |
| `--no-prompts` | Hide your own typed prompts (for screen sharing) |
| `--plain` | No colors or hyperlink escape sequences |
| `--links auto/on/off` | Terminal hyperlink preference |
| `--log-root /path/to/projects` | Use an alternate Claude Code projects directory |

Stop with **Ctrl+C**. This stops only ClaudeWatch, not your agents.

## File links

Existing local files referenced in messages can be opened with Ctrl+click in Windows
Terminal. Other terminals may use a different gesture. Files are never opened
automatically. Plain terminals show copyable paths. Links do not navigate to source
line numbers. Relative references use the session's working directory.

Launching in an older Command Prompt or PowerShell console does not add hyperlink
support, even as administrator. Install Windows Terminal to use clickable links.
For scripts or an intentional launch in the current console, set
`CLAUDEWATCH_CURRENT_CONSOLE=1` before running the batch launcher. `--links on`
only emits link sequences; the terminal still needs to support them.

Network/UNC targets are excluded. Detection is limited to 2,048 candidates and 128
existing files per record. Ambiguous bare filenames cannot safely identify a target.

## Privacy and safety

ClaudeWatch has no telemetry, network client, credentials access, or model calls.
It reads local transcript files and checks local referenced paths. The only subprocess
is a fixed, read-only `git worktree list` invocation using an argument list; log text
is never executed or passed to a shell. Git is located only through absolute `PATH`
entries (never the current folder), Git's repository ownership checks stay active, and
`core.fsmonitor` is disabled so repository configuration cannot start a program.
Git is optional.

The viewer uses an allowlist. It shows prompts you typed, assistant text, saved thinking
text, tool calls and results. It never shows injected context: system reminders,
attachments (CLAUDE.md, memory, skill and tool listings, hook output), meta and compaction
summary messages, file-history snapshots, account and bridge records, thinking signatures,
or redacted thinking. Session metadata keeps only display and matching fields such as the
working folder, title, and agent type.

Recorded terminal escape sequences are stripped. Hyperlinks are generated from validated
existing local paths, not copied from log escape sequences. Malformed JSON, invalid display
fields, and unpaired Unicode surrogates are skipped with a notice so later records can still
be displayed.

**Displayed prompts, agent messages, thinking, tool output, and paths can still contain
private project information.** Use `--no-prompts` and `--no-thinking` when sharing your
screen, and review terminal captures before sharing them. This is a viewer, not a redaction
tool. Clicking a file uses your OS associations, including executable file types.

## Resource handling and limitations

Records are streamed one at a time. The session index and agent label cache retain
at most 4,096 recent transcripts; tool-call tracking retains at most 256 entries.
Duplicate-event hashes retain at most 512 entries. Startup history has a count limit
and a 2-million-character text budget, with a 16,384-character preview per entry, and is
released after display. Only the first 64 lines (1 MiB) of each transcript are scanned for
metadata.

Individual records over 8 MiB are skipped with a notice during live watching. Incomplete
lines are retried, and oversized incomplete lines are discarded in bounded chunks.
These limits are safeguards, not a guarantee of an exact process RAM limit.

Logs are polled every second, new sessions every four seconds, Git worktrees at most
once per minute. ClaudeWatch writes no activity logs.

The viewer follows sessions started in the selected folder, its descendants (including
`.claude/worktrees/`), registered Git worktrees, and all subagents of matching sessions.
Old sessions are included; a saved file does not prove an agent is running. Live events
preserve order within each transcript; different transcripts are read in polling order.

"Answer" means the assistant's text ended its turn (`stop_reason: end_turn`). Other
assistant text is shown as "Progress". Cloud sessions without local transcripts and
unsaved in-flight activity are unavailable. Error detection uses the recorded `is_error`
flag, the denial kind, and the exit code, so it is more precise than text heuristics.
It is still a summary; use `--details` to assess the actual result.

The Claude Code transcript format is internal and may change. Compatibility is based on
observed `user`, `assistant`, and `system` records and the `subagents/` layout, not a
stable API.

## Development

```sh
python -m unittest discover -s tests -v
python -m ruff check .
python -m ruff format --check .
```

Functional tests use synthetic transcripts and temporary files, without contacting any
service. CI runs on Windows, Linux, and macOS with Python 3.11 and 3.13.

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE). Independent community utility; not an official Anthropic product.
