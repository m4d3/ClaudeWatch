"""ClaudeWatch: read-only human-focused viewer of local Claude Code session activity."""

import argparse
import collections
import heapq
import hashlib
import datetime as dt
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import textwrap
import time
from typing import NamedTuple
from urllib.parse import unquote, urlsplit

TAIL_BYTES = 2 * 1024 * 1024
MAX_RECORD_BYTES = 8 * 1024 * 1024
HISTORY_BYTES = 2 * 1024 * 1024
HEAD_BYTES = 1024 * 1024
HEAD_LINES = 64
MAX_SESSIONS = 4096
MALFORMED_NOTICE = "Malformed record skipped; inspect original log for details."
RECORD_ERRORS = (TypeError, ValueError, AttributeError, RecursionError, UnicodeError)

# Tools whose calls are conversations between agents or with the user, not routine actions.
COORDINATION = {
    "Agent",
    "Task",
    "SendMessage",
    "AskUserQuestion",
    "SubagentHandback",
    "TaskStop",
}
PLAN_TOOLS = {"TodoWrite", "ExitPlanMode", "TaskCreate", "TaskUpdate"}
FILE_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
DENIALS = {
    "permission-rule": "Denied by a permission rule",
    "user-rejected": "Rejected by the user",
    "automode-blocked": "Blocked by auto mode",
    "automode-unavailable": "Auto mode classifier unavailable",
    "cancelled": "Cancelled",
}
SYSTEM_TAGS = r"<(?:local-command-|command-|bash-std|system|user-memory|task-)"
IMAGE_EXT = (".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg", ".tif", ".tiff")


class Event(NamedTuple):
    """One displayable unit; a Claude Code record can hold several content blocks."""

    kind: str
    body: str
    name: str = ""
    call: str = ""
    severity: str = ""


def clean(value):
    text = str(value)
    text = re.sub(r"[\ud800-\udfff]", "\ufffd", text)
    # Recorded commands/results are data: do not let escape sequences control the terminal.
    text = re.sub(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)", "", text)
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", "", text)


def norm(path):
    text = str(path)
    if text.startswith("\\\\?\\"):
        text = text[4:]
    return os.path.normcase(os.path.abspath(text)).rstrip("\\/")


def under(path, root):
    if not path:
        return False
    p, r = norm(path), norm(root)
    return p == r or p.startswith(r + os.sep)


def project_key(path):
    """Claude Code names each project folder after its path with non-alphanumerics as '-'."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def text_parts(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (text_parts(x) for x in value)))
    if isinstance(value, dict):
        if value.get("type") in ("image", "document"):
            return f"[{value['type'].title()} attachment; view it in Claude Code]"
        for key in ("text", "content"):
            if key in value:
                return text_parts(value[key])
    return ""


def show_value(value):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return value
    return json.dumps(value, ensure_ascii=False, indent=2)


def tag_value(text, tag):
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, re.S)
    return match.group(1).strip() if match else ""


def result_severity(record, block, text):
    """Real failures are red; refusals, guard messages, and interruptions are amber."""
    if record.get("toolDenialKind"):
        return "attention"
    if not block.get("is_error"):
        result = record.get("toolUseResult")
        if isinstance(result, dict) and result.get("interrupted"):
            return "attention"
        return ""
    head = text.lstrip()[:4096]
    if re.match(r"Exit code (?:-|[1-9])", head) or re.search(
        r"(?m)^\s*(?:Traceback \(most recent|fatal\b|\w*(?:Error|Exception)\s*:)", head
    ):
        return "error"
    return "attention"


def visible(record, thinking=True, prompts=True):
    """Skip malformed records without preventing subsequent activity from displaying."""
    try:
        if not isinstance(record, dict):
            raise TypeError("Expected an object")
        stamp = record.get("timestamp")
        if stamp is not None and not isinstance(stamp, str):
            raise TypeError("Expected a timestamp string")
        message = record.get("message")
        if message is not None and not isinstance(message, dict):
            raise TypeError("Expected a message object")
        events = _visible(record, thinking, prompts)
        for event in events:
            for field in event:
                if not isinstance(field, str):
                    raise TypeError("Expected display text")
                field.encode("utf-8")
        return events
    except RECORD_ERRORS:
        return [Event("VIEWER NOTICE", MALFORMED_NOTICE)]


def _visible(record, thinking, prompts):
    """Allowlist user-visible data. Never display system context, hooks, or signatures."""
    kind = record.get("type")
    message = record.get("message") or {}
    content = message.get("content")
    if kind == "viewer_notice":
        return [Event("VIEWER NOTICE", str(record.get("text") or "Log record skipped"))]
    if kind == "assistant":
        blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
        if record.get("isApiErrorMessage"):
            return [Event("STATUS", text_parts(blocks) or "API error", severity="error")]
        phase = "answer" if message.get("stop_reason") == "end_turn" else "progress"
        events = []
        for block in blocks:
            if not isinstance(block, dict):
                raise TypeError("Expected a content block")
            block_type = block.get("type")
            # Redacted thinking and signatures are opaque; only saved thinking text is shown.
            if block_type == "thinking" and thinking and block.get("thinking"):
                events.append(Event("THINKING", block["thinking"]))
            elif block_type == "text" and block.get("text"):
                events.append(Event("MESSAGE " + phase, block["text"]))
            elif block_type in ("tool_use", "server_tool_use"):
                name = block.get("name")
                name = name if isinstance(name, str) and name else "tool"
                call = block.get("id")
                call = call if isinstance(call, str) else ""
                events.append(Event("ACTION", show_value(block.get("input", {})), name, call))
        return events
    if kind == "user":
        if isinstance(content, list):
            events = []
            for block in content:
                if not isinstance(block, dict):
                    raise TypeError("Expected a content block")
                if block.get("type") == "tool_result":
                    text = text_parts(block.get("content", ""))
                    text = re.sub(r"</?tool_use_error>", "", text)
                    call = block.get("tool_use_id")
                    call = call if isinstance(call, str) else ""
                    severity = result_severity(record, block, text)
                    events.append(Event("RESULT", text, call=call, severity=severity))
            if events:
                return events
        if record.get("isCompactSummary") or record.get("toolUseResult") is not None:
            return []
        text = text_parts(content).strip()
        if text.startswith("<task-notification"):
            status = tag_value(text, "status") or "update"
            summary = tag_value(text, "summary") or "Background task " + status
            severity = {"failed": "error", "killed": "attention"}.get(status, "")
            return [Event("AGENT ACTIVITY", clean(summary), status, severity=severity)]
        origin = record.get("origin")
        origin = origin.get("kind") if isinstance(origin, dict) else None
        if origin in ("peer", "coordinator") and text:
            return [Event("AGENT MESSAGE", text, "from " + origin)]
        # A subagent's first prompt is already shown from the parent's Agent call.
        if record.get("isMeta") or (record.get("isSidechain") and not record.get("parentUuid")):
            return []
        if text.startswith("<command-name>"):
            command = (
                tag_value(text, "command-name") + " " + tag_value(text, "command-args")
            ).strip()
            return [Event("PROMPT", command)] if prompts and command else []
        # Injected context is not something the person typed.
        text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()
        if not text or not prompts or re.match(SYSTEM_TAGS, text):
            return []
        return [Event("PROMPT", text)]
    if kind == "system":
        subtype = record.get("subtype")
        if subtype == "compact_boundary":
            return [Event("STATUS", "Conversation compacted")]
        if subtype == "stop_hook_summary" and record.get("hookErrors"):
            return [Event("STATUS", "A stop hook reported an error", severity="attention")]
        level = record.get("level")
        if level in ("error", "warning") and isinstance(record.get("content"), str):
            return [
                Event(
                    "STATUS",
                    record["content"],
                    severity="error" if level == "error" else "attention",
                )
            ]
    return []


def read_head(path):
    """Collect display and matching metadata from the start of a transcript, never prompts."""
    meta = {}
    try:
        with path.open("rb") as f:
            budget = HEAD_BYTES
            for _ in range(HEAD_LINES):
                line = f.readline(budget)
                budget -= len(line)
                if not line.endswith(b"\n") or budget <= 0:
                    break
                try:
                    row = json.loads(line)
                except (ValueError, UnicodeError, RecursionError):
                    continue
                if not isinstance(row, dict):
                    continue
                for src, dst in (
                    ("cwd", "cwd"),
                    ("sessionId", "session"),
                    ("agentId", "agent_id"),
                    ("timestamp", "timestamp"),
                ):
                    if isinstance(row.get(src), str) and dst not in meta:
                        meta[dst] = row[src]
                if row.get("type") == "custom-title" and isinstance(row.get("customTitle"), str):
                    meta["title"] = row["customTitle"]
    except OSError:
        pass
    return meta


class Log:
    """One transcript cursor with minimal metadata; reads only complete bounded records."""

    def __init__(self, path, meta):
        self.path = path
        self.meta = {
            k: meta[k]
            for k in (
                "cwd",
                "session",
                "agent_id",
                "timestamp",
                "title",
                "agent_type",
                "description",
            )
            if k in meta and isinstance(meta[k], str)
        }
        self.subagent = path.parent.name == "subagents"
        if self.subagent:
            self.id = self.meta.get("agent_id") or path.stem.removeprefix("agent-")
            self.parent = self.meta.get("session") or path.parent.parent.name
            self.project = path.parent.parent.parent.name
        else:
            self.id = self.meta.get("session") or path.stem
            self.parent = None
            self.project = path.parent.name
        self.offset = 0
        self.discarding = False
        self.label = ("subagent " if self.subagent else "main ") + self.id

    def records(self, initial=False, replay=False):
        # Offset advances only over complete lines; partial UTF-8/JSON is retried.
        try:
            size = self.path.stat().st_size
            if size < self.offset:
                self.offset = 0
                self.discarding = False
            with self.path.open("rb") as f:
                if initial:
                    f.seek(max(0, size - TAIL_BYTES))
                    if f.tell():
                        self.discarding = not f.readline(MAX_RECORD_BYTES + 1).endswith(b"\n")
                else:
                    f.seek(self.offset)
                while True:
                    start = f.tell()
                    line = f.readline(MAX_RECORD_BYTES + 1)
                    if len(line) > MAX_RECORD_BYTES or self.discarding:
                        first_chunk = not self.discarding
                        self.discarding = not line.endswith(b"\n")
                        self.offset = f.tell()
                        if first_chunk and not initial:
                            yield {
                                "type": "viewer_notice",
                                "text": "Oversized record skipped; inspect original log for details.",
                            }
                        if not line:
                            break
                        continue
                    if not line or not line.endswith(b"\n"):
                        self.offset = start
                        break
                    self.offset = f.tell()
                    if initial and not replay:
                        continue
                    try:
                        record = json.loads(line)
                    except (ValueError, UnicodeError, RecursionError):
                        record = None
                    if isinstance(record, dict):
                        if not self.meta.get("cwd") and isinstance(record.get("cwd"), str):
                            self.meta["cwd"] = record["cwd"]
                        yield record
                    else:
                        yield {"type": "viewer_notice", "text": MALFORMED_NOTICE}
        except (OSError, PermissionError):
            return


class Catalog:
    """Discover recent transcripts and associate projects, worktrees, and subagents."""

    def __init__(self, roots):
        self.roots, self.logs = roots, {}

    def discover(self):
        def scan_paths():
            for root in self.roots:
                for folder, _, files in os.walk(root):
                    for name in files:
                        if name.endswith(".jsonl"):
                            path = Path(folder) / name
                            try:
                                yield (path.stat().st_mtime, str(path), path)
                            except OSError:
                                continue

        recent = heapq.nlargest(MAX_SESSIONS, scan_paths())
        allowed = {entry[2] for entry in recent}
        self.logs = {p: log for p, log in self.logs.items() if p in allowed}
        for _, _, path in recent:
            log = self.logs.get(path)
            if log is not None:
                # A brand-new session may not have written its working folder yet.
                if not log.meta.get("cwd") and not log.offset:
                    cwd = read_head(path).get("cwd")
                    if cwd:
                        log.meta["cwd"] = cwd
                continue
            meta = read_head(path)
            if path.parent.name == "subagents":
                try:
                    side = path.with_name(path.stem + ".meta.json")
                    with side.open("rb") as f:
                        extra = json.loads(f.read(64 * 1024))
                    if isinstance(extra, dict):
                        for src, dst in (
                            ("agentType", "agent_type"),
                            ("description", "description"),
                        ):
                            if isinstance(extra.get(src), str):
                                meta[dst] = extra[src]
                except (OSError, ValueError, UnicodeError, RecursionError):
                    pass
            self.logs[path] = Log(path, meta)

    def matching(self, folders):
        keys = {project_key(folder) for folder in folders}

        def direct(log):
            cwd = log.meta.get("cwd")
            if cwd:
                return any(under(cwd, folder) for folder in folders)
            # Without a recorded folder, fall back to Claude Code's project directory name.
            return log.project in keys

        matched = {log.id for log in self.logs.values() if direct(log)}
        # Include subagents even if they work in another checkout or temporary directory.
        while True:
            added = {log.id for log in self.logs.values() if log.parent in matched}
            if added <= matched:
                break
            matched.update(added)
        return [log for log in self.logs.values() if log.id in matched]


def find_git():
    """Search only absolute PATH entries: Windows would otherwise try the current folder."""
    names = ("git.exe",) if os.name == "nt" else ("git",)
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip('"')
        if not folder or not os.path.isabs(folder):
            continue
        for name in names:
            candidate = os.path.join(folder, name)
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
    return None


def worktrees(repo):
    git = find_git()
    if not git:
        return []
    try:
        # Git's ownership checks stay active; fsmonitor is off so repo config runs nothing.
        result = subprocess.run(
            [
                git,
                "-c",
                "core.fsmonitor=false",
                "-C",
                str(repo),
                "worktree",
                "list",
                "--porcelain",
                "-z",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=5,
            check=True,
        )
        return [
            Path(x[9:])
            for x in result.stdout.decode("utf-8", "replace").split("\0")
            if x.startswith("worktree ")
        ]
    except (OSError, subprocess.SubprocessError):
        return []


def pick_folder(catalog):
    recent = {}
    for log in catalog.logs.values():
        cwd = log.meta.get("cwd")
        if cwd and not log.subagent and Path(cwd).is_dir():
            stamp = str(log.meta.get("timestamp", ""))
            recent[cwd] = max(recent.get(cwd, ""), stamp)
    choices = sorted(recent, key=recent.get, reverse=True)[:15]
    print("\nChoose a project (recent Claude Code folders):\n")
    for n, folder in enumerate(choices, 1):
        print(f"  {n:2}. {clean(folder)}")
    print("\n   B. Browse for a folder\n   Q. Quit\nOr paste a folder path.")
    while True:
        value = input("\nFolder / number [B]: ").strip().strip('"')
        if value.lower() == "q":
            return None
        if not value or value.lower() == "b":
            try:
                import tkinter as tk
                from tkinter import filedialog

                window = tk.Tk()
                window.withdraw()
                window.attributes("-topmost", True)
                value = filedialog.askdirectory(title="Choose project to watch", parent=window)
                window.destroy()
                if not value:
                    continue
            except Exception as exc:
                print(f"Folder picker unavailable: {clean(exc)}. Paste a path instead.")
                continue
        if value.isdigit() and 1 <= int(value) <= len(choices):
            value = choices[int(value) - 1]
        path = Path(os.path.expandvars(value)).expanduser()
        if path.is_dir():
            return path.absolute()
        print("That folder does not exist. Try again.")


def local_time(stamp):
    if not isinstance(stamp, str):
        return ""
    try:
        return (
            dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            .astimezone()
            .strftime("%H:%M:%S")
        )
    except (ValueError, OverflowError, OSError):
        return clean(stamp)


def readable(value, level=0):
    """Unpack nested JSON tool envelopes without removing command/result text."""
    if level > 12:
        return show_value(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (ValueError, RecursionError):
            return value
        if isinstance(parsed, (dict, list)):
            return readable(parsed, level + 1)
        return value
    if isinstance(value, list):
        return "\n\n".join(readable(x, level + 1) for x in value)
    if isinstance(value, dict):
        if value.get("type") in ("text", "output_text", "input_text"):
            return readable(value.get("text", ""), level + 1)
        if value.get("type") in ("image", "audio", "document"):
            return f"[{value['type'].title()} attachment; view it in Claude Code]"
        parts = []
        for key, item in value.items():
            if key in ("annotations", "_meta", "signature"):
                continue
            label = {
                "file_path": "File",
                "old_string": "Replace",
                "new_string": "With",
                "subagent_type": "Agent type",
                "run_in_background": "Background",
            }.get(key, key.replace("_", " ").capitalize())
            body = readable(item, level + 1)
            if key in ("output", "content", "text", "input", "arguments"):
                parts.append(body)
            elif "\n" in body:
                parts.append(label + ":\n" + textwrap.indent(body, "  "))
            else:
                parts.append(label + ": " + body)
        return "\n".join(parts)
    return str(value)


def file_references(text, cwd):
    """Resolve explicit references to existing local files; never open anything."""
    candidates = []
    for match in re.finditer(r"!?\[[^\]\n]*\]\((?:<([^>]+)>|([^\s)]+))\)", text):
        candidates.append(match.group(1) or match.group(2))
    candidates.extend(re.findall(r"`([^`\n]+)`", text))
    candidates.extend(m.group(2) for m in re.finditer(r"""(["'])([^\n]*?)\1""", text))
    for line in text.splitlines():
        candidates.append(line.strip())
        if ": " in line:
            candidates.append(line.split(": ", 1)[1].strip())
    candidates.extend(
        re.findall(r"""(?:file:///[A-Za-z]:[^\s<>"')]*|[A-Za-z]:[\\/][^\s<>"')]+)""", text)
    )
    candidates.extend(
        re.findall(r"(?<![\w:])(?:[\w.-]+[\\/])*[\w.-]+\.[A-Za-z0-9]{1,10}(?!\w)", text)
    )
    found = []
    seen = set()
    for candidate in candidates[:2048]:
        candidate = candidate.strip().strip('`<>"').rstrip(".,;")
        if len(candidate) > 4096:
            continue
        candidate = re.sub(r":\d+(?::\d+)?$", "", candidate)
        if candidate.startswith("file:"):
            try:
                uri = urlsplit(candidate)
            except (ValueError, RecursionError):
                continue
            if uri.netloc not in ("", "localhost"):
                continue
            candidate = unquote(uri.path)
            if re.match(r"^/[A-Za-z]:", candidate):
                candidate = candidate[1:]
        elif re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", candidate) and not re.match(
            r"^[A-Za-z]:[\\/]", candidate
        ):
            continue
        if candidate.startswith("\\\\?\\"):
            candidate = candidate[4:]
        if re.match(r"^/[A-Za-z]:[\\/]", candidate):
            candidate = candidate[1:]
        if (
            not candidate
            or candidate.startswith(("\\\\", "//"))
            or any(ord(c) < 32 for c in candidate)
        ):
            continue
        try:
            path = Path(candidate)
            if not path.is_absolute():
                if not cwd:
                    continue
                path = Path(cwd) / path
            path = path.resolve()
            if str(path).startswith(("\\\\", "//")):
                continue
            if path.is_file() and norm(path) not in seen:
                seen.add(norm(path))
                found.append(path)
                if len(found) >= 128:
                    break
        except (OSError, ValueError):
            continue
    return found


def tool_label(name):
    if name.startswith("mcp__"):
        server, _, tool = name[5:].partition("__")
        server = re.sub(r"^[0-9a-f]{8}-[0-9a-f-]{27}$", "connector", server)
        return (server + " · " + tool.replace("_", " ")) if tool else server
    return name


def arguments(body):
    try:
        args = json.loads(body)
    except (ValueError, RecursionError):
        return {}
    return args if isinstance(args, dict) else {}


def action_summary(args):
    for key in (
        "description",
        "command",
        "file_path",
        "notebook_path",
        "pattern",
        "url",
        "query",
        "skill",
        "path",
    ):
        value = args.get(key)
        if isinstance(value, str) and value.strip():
            return next(x for x in value.splitlines() if x.strip()).strip()
    return ""


def coordination_text(name, args):
    """Turn agent coordination calls into the message a person would want to read."""
    if name in ("Agent", "Task"):
        target = args.get("subagent_type") or "general-purpose"
        head = f"To: {target}"
        if args.get("description"):
            head += f" · {args['description']}"
        if args.get("run_in_background"):
            head += " · background"
        return head + "\n" + str(args.get("prompt") or "")
    if name == "SendMessage":
        target = args.get("to") or args.get("recipient")
        message = args.get("message") or args.get("content") or args.get("summary") or ""
        return (f"To: {target}\n" if target else "") + readable(message)
    if name == "AskUserQuestion" and args.get("questions"):
        lines = []
        for question in args["questions"]:
            if isinstance(question, dict):
                lines.append(str(question.get("question", "")))
                for option in question.get("options") or []:
                    if isinstance(option, dict):
                        lines.append("  - " + str(option.get("label", "")))
        return "\n".join(lines)
    return readable(args)


def plan_text(name, args):
    if name == "TodoWrite" and isinstance(args.get("todos"), list):
        marks = {"completed": "[x]", "in_progress": "[>]"}
        return "\n".join(
            f"{marks.get(todo.get('status'), '[ ]')} {todo.get('content', '')}"
            for todo in args["todos"]
            if isinstance(todo, dict)
        )
    if name == "ExitPlanMode" and args.get("plan"):
        return str(args["plan"])
    if name in ("TaskCreate", "TaskUpdate"):
        title = args.get("subject") or args.get("title") or args.get("taskId") or "Task"
        status = args.get("status")
        return f"{title}" + (f" · {status}" if status else "")
    return readable(args)


def preview(event, limit):
    """Shorten a history entry; tool arguments stay valid JSON so summaries still work."""
    if len(event.body) <= limit:
        return event.body
    note = "[Startup preview shortened; see original log.]"
    if event.kind == "ACTION":
        args = arguments(event.body)
        if args:
            args = {
                k: (
                    v[: limit // 4] + "\n" + note
                    if isinstance(v, str) and len(v) > limit // 4
                    else v
                )
                for k, v in args.items()
            }
            body = show_value(args)
            if len(body) <= limit:
                return body
    return event.body[:limit] + "\n" + note


class ConsoleView:
    """Format the human feed and keep bounded labels, calls, and deduplication state."""

    COLORS = {
        "Prompt": "1;97",
        "Thinking": "96",
        "Progress": "94",
        "Answer": "1;92",
        "Action": "33",
        "Output": "37",
        "Agent message": "95",
        "Agent activity": "95",
        "Plan": "96",
        "Status": "90",
        "Error": "1;91",
        "Attention": "33",
    }

    def __init__(
        self,
        full=False,
        plain=False,
        raw=False,
        max_lines=28,
        links="auto",
        details=False,
        color_mode="auto",
    ):
        self.full, self.raw, self.max_lines = full, raw, max_lines
        self.details = details or full or raw
        self.calls = collections.OrderedDict()
        self.recent_entries = collections.OrderedDict()
        self.tags, self.names = collections.OrderedDict(), {}
        self.next_tag = 1
        self.width = max(40, min(110, shutil.get_terminal_size((100, 30)).columns - 2))
        self.color = (
            sys.stdout.isatty()
            and not plain
            and color_mode != "off"
            and (color_mode == "on" or "NO_COLOR" not in os.environ)
        )
        if self.color and os.name == "nt":
            try:
                import ctypes

                kernel = ctypes.windll.kernel32
                # Declare pointer-sized handles: ctypes otherwise assumes a 32-bit int.
                kernel.GetStdHandle.argtypes = [ctypes.c_uint]
                kernel.GetStdHandle.restype = ctypes.c_void_p
                kernel.GetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint)]
                kernel.GetConsoleMode.restype = ctypes.c_int
                kernel.SetConsoleMode.argtypes = [ctypes.c_void_p, ctypes.c_uint]
                kernel.SetConsoleMode.restype = ctypes.c_int
                handle = kernel.GetStdHandle(-11)
                mode = ctypes.c_uint()
                self.color = bool(
                    kernel.GetConsoleMode(handle, ctypes.byref(mode))
                    and kernel.SetConsoleMode(handle, mode.value | 4)
                )
            except (OSError, AttributeError):
                self.color = False
        supported = bool(
            os.environ.get("WT_SESSION")
            or os.environ.get("TERM_PROGRAM") in ("vscode", "WezTerm", "iTerm.app")
            or os.environ.get("TERM", "").startswith("xterm-kitty")
        )
        self.links = (
            sys.stdout.isatty() and not plain and (links == "on" or (links == "auto" and supported))
        )

    def hyperlink(self, path, label):
        label = clean(label)
        if not self.links:
            return label
        return f"\x1b]8;;{path.as_uri()}\x1b\\{label}\x1b]8;;\x1b\\"

    def linked_text(self, text, paths, cwd):
        """Protect filename spans while wrapping and remove redundant standalone links."""
        tokens, aliases = {}, {}
        name_counts = collections.Counter(path.name for path in paths)
        for index, path in enumerate(paths[:128]):
            token = "\ue000" + str(index).zfill(max(4, len(path.name) - 2)) + "\ue001"
            tokens[token] = (path, path.name)
            for alias in (str(path), path.as_posix(), path.as_uri()):
                aliases[alias] = token
            if name_counts[path.name] == 1:
                aliases[path.name] = token
            else:
                tokens[token] = (path, path.parent.name + "/" + path.name)
            try:
                relative = path.relative_to(Path(cwd))
                aliases[str(relative)] = aliases[relative.as_posix()] = token
            except (ValueError, TypeError):
                pass

        def markdown(match):
            target = (match.group(2) or match.group(3)).strip()
            target = re.sub(r":\d+(?::\d+)?$", "", target)
            return aliases.get(target, match.group(0))

        text = re.sub(r"!?\[([^\]\n]*)\]\((?:<([^>]+)>|([^\s)]+))\)", markdown, text)
        if aliases:
            pattern = "|".join(re.escape(x) for x in sorted(aliases, key=len, reverse=True))
            text = re.sub(
                r"(?<![\w])(?:" + pattern + r")(?![\w])", lambda m: aliases[m.group(0)], text
            )
        seen, output = set(), []
        for line in text.splitlines():
            key = line.strip().strip("`")
            if key in tokens and key in seen:
                continue
            seen.add(key)
            output.append(line)
        return "\n".join(output), tokens

    def expand_links(self, text, tokens):
        for token, (path, label) in tokens.items():
            text = text.replace(token, self.hyperlink(path, label if self.links else str(path)))
        return text

    def is_duplicate(self, log, record, event):
        """Exact-event deduplication with a fixed-size cache; later updates remain visible."""
        data = "\n".join((event.kind, event.call, event.body)).encode("utf-8")
        digest = hashlib.blake2b(data, digest_size=16).digest()
        key = (log.id, record.get("timestamp") or "", digest)
        if key in self.recent_entries:
            return True
        self.recent_entries[key] = True
        while len(self.recent_entries) > 512:
            self.recent_entries.popitem(last=False)
        return False

    def attachments(self, content, log):
        paths = file_references(content, log.meta.get("cwd"))
        if not paths:
            return
        self.notice(
            "  Referenced files"
            + (" · Ctrl+click to open" if self.links else " · copy a path to open")
        )
        for path in paths:
            kind = "Image" if path.suffix.lower() in IMAGE_EXT else "File"
            print("  " + self.paint(kind + "  ", "36") + self.hyperlink(path, path.name))
            self.notice("        " + str(path))

    def paint(self, text, color):
        return f"\x1b[{color}m{text}\x1b[0m" if self.color else text

    def agent(self, log):
        if log.id not in self.tags:
            if len(self.tags) >= MAX_SESSIONS:
                oldest, _ = self.tags.popitem(last=False)
                self.names.pop(oldest, None)
            self.tags[log.id] = f"A{self.next_tag:02}"
            self.next_tag += 1
            if log.subagent:
                name = (log.meta.get("agent_type") or "subagent").replace("-", " ")
                name = name.replace("_", " ").capitalize()
                description = log.meta.get("description")
                if description:
                    name += f" ({description[:48]})"
            else:
                name = "Main session"
                if log.meta.get("title"):
                    name += f" ({log.meta['title'][:48]})"
            self.names[log.id] = clean(name)
        return self.tags[log.id] + "  " + self.names[log.id]

    def notice(self, text):
        print(self.paint(clean(text), "90"), flush=True)

    def short(self, text, limit=None):
        text = " ".join(clean(text).split())
        limit = limit or max(20, self.width - 24)
        return text if len(text) <= limit else text[: limit - 1] + "…"

    def translate(self, log, record, event):
        """Track tool names across calls/results and convert coordination into messages."""
        key = (log.id, event.call)
        if event.kind == "ACTION":
            self.calls[key] = event.name
            while len(self.calls) > 256:
                self.calls.popitem(last=False)
            if event.name in COORDINATION:
                text = coordination_text(event.name, arguments(event.body))
                return event._replace(kind="AGENT MESSAGE", body=text, name=event.name)
            if event.name in PLAN_TOOLS:
                return event._replace(
                    kind="PLAN", body=plan_text(event.name, arguments(event.body))
                )
        elif event.kind == "RESULT":
            event = event._replace(name=self.calls.pop(key, event.name))
        return event

    def focused(self, log, record, event):
        """Human conversation gets space; routine machine detail gets one line."""
        kind, body, name, severity = event.kind, event.body, event.name, event.severity
        result = record.get("toolUseResult")
        result = result if isinstance(result, dict) else {}
        if kind == "RESULT" and not severity:
            if name in PLAN_TOOLS:
                return True
            # Agent reports and the user's answers are conversation; other results are receipts.
            if name in ("Agent", "Task") and result.get("status") == "completed":
                return False
            if name == "AskUserQuestion":
                return False
        if kind in ("PROMPT", "THINKING", "AGENT MESSAGE", "PLAN") or kind.startswith("MESSAGE"):
            return False
        stamp = local_time(record.get("timestamp"))
        tag = self.agent(log).split("  ", 1)[0]
        content = clean(readable(body)).strip()
        lines = content.splitlines()
        if kind == "STATUS" and not severity:
            self.notice(f"  {stamp}  {tag}  {self.short(content)}")
            return True
        label = tool_label(name) if name else "tool"
        detail_lines = [x for x in lines if x.strip()]
        if severity:
            title = "ERROR" if severity == "error" else "ATTENTION"
            exit_code = re.match(r"Exit code (-?\d+)", content)
            denial = DENIALS.get(record.get("toolDenialKind"))
            if kind in ("STATUS", "AGENT ACTIVITY"):
                summary, detail_lines = (detail_lines or ["Reported"])[0], detail_lines[1:]
            elif exit_code:
                summary, detail_lines = f"{label} · exit {exit_code.group(1)}", detail_lines[1:]
            elif denial or result.get("interrupted"):
                summary = f"{label} · {denial or 'interrupted'}"
            else:
                summary, detail_lines = f"{label} · " + (detail_lines or [""])[0], detail_lines[1:]
        elif kind == "ACTION":
            args = arguments(body)
            title = "Files" if name in FILE_TOOLS else "Action"
            summary = label
            first = action_summary(args)
            if first:
                summary += " · " + first
        elif kind == "AGENT ACTIVITY":
            title, summary = "Agent", f"{name or 'Background task'} · {content}"
        elif kind == "RESULT":
            title = "Result"
            summary = label
            if name in ("Agent", "Task") and result.get("status") == "async_launched":
                summary += " · launched in background"
            elif result.get("returnCodeInterpretation"):
                summary += " · " + str(result["returnCodeInterpretation"])
            summary += f" · {len(lines)} lines"
        else:
            title, summary = "Activity", lines[0] if lines else "Recorded"
        limit = max(20, self.width - len(f"  {stamp}  {tag}  {title}: "))
        paths = file_references(content, log.meta.get("cwd"))
        summary, tokens = self.linked_text(self.short(summary, limit), paths, log.meta.get("cwd"))
        color = {
            "Action": "33",
            "Result": "90",
            "Files": "35",
            "Agent": "95",
            "ATTENTION": "33",
            "ERROR": "1;91",
        }.get(title, "90")
        print(
            self.paint(f"  {stamp}  ", "90")
            + self.paint(tag, "1")
            + "  "
            + self.paint(title + ": ", color)
            + self.paint(self.expand_links(summary, tokens), "91" if severity == "error" else "90"),
            flush=True,
        )
        for line in detail_lines[:3] if severity else ():
            print(
                self.paint(
                    "    " + self.short(line, self.width - 6),
                    "91" if severity == "error" else "33",
                )
            )
        linked_paths = {path for token, (path, _) in tokens.items() if token in summary}
        for path in paths:
            if path.suffix.lower() in IMAGE_EXT and path not in linked_paths:
                print("    Image: " + self.hyperlink(path, path.name if self.links else str(path)))
        return True

    def emit(self, log, record, event):
        # Keep data/format failures scoped to one record, including during history replay.
        try:
            self._emit(log, record, event)
        except RECORD_ERRORS:
            self.notice(MALFORMED_NOTICE)

    def _emit(self, log, record, event):
        if event.kind == "VIEWER NOTICE":
            self.notice(event.body)
            return
        if self.is_duplicate(log, record, event):
            return
        if self.raw:
            label = event.kind + (f" {event.name}" if event.name else "")
            label += f" [{event.call}]" if event.call else ""
            label += f" ({event.severity})" if event.severity else ""
            print(f"\n[{local_time(record.get('timestamp'))}] [{clean(log.label)}] {clean(label)}")
            print(clean(event.body), flush=True)
            self.attachments(clean(readable(event.body)), log)
            return
        event = self.translate(log, record, event)
        if not self.details and self.focused(log, record, event):
            return
        kind = event.kind
        title = {
            "PROMPT": "Prompt",
            "THINKING": "Thinking",
            "MESSAGE progress": "Progress",
            "MESSAGE answer": "Answer",
            "ACTION": "Action",
            "RESULT": "Output",
            "AGENT MESSAGE": "Agent message",
            "AGENT ACTIVITY": "Agent activity",
            "PLAN": "Plan",
            "STATUS": "Status",
        }.get(kind, "Message")
        detail = ""
        if kind in ("ACTION", "RESULT", "AGENT MESSAGE", "AGENT ACTIVITY") and event.name:
            detail = tool_label(event.name)
        if kind == "RESULT" and event.name in ("Agent", "Task") and not event.severity:
            title, detail = "Agent message", "report"
        if event.severity:
            detail = (title + (" · " + detail if detail else "")).lower()
            title = "Error" if event.severity == "error" else "Attention"
        content = clean(readable(event.body)).strip()
        paths = file_references(content, log.meta.get("cwd"))
        content, tokens = self.linked_text(content, paths, log.meta.get("cwd"))
        color = self.COLORS.get(title, "36")
        stamp = local_time(record.get("timestamp"))
        if title == "Status":
            print(self.paint(f"  {stamp}  {self.agent(log)}  |  {content}", "90"), flush=True)
            return
        print("\n" + self.paint("─" * self.width, color))
        print(self.paint(f"  {stamp}  {self.agent(log)}", "1"))
        print(self.paint("  " + title + (" · " + clean(detail) if detail else ""), color))
        print()
        lines = []
        for line in content.splitlines() or ["(No text recorded)"]:
            lines.extend(
                textwrap.wrap(
                    line.expandtabs(4),
                    width=self.width - 4,
                    replace_whitespace=False,
                    drop_whitespace=False,
                    break_long_words=False,
                    break_on_hyphens=False,
                )
                or [""]
            )
        hidden = 0
        if not self.full and len(lines) > self.max_lines:
            hidden = len(lines) - self.max_lines
            lines = lines[: self.max_lines]
        for line in lines:
            print("  " + self.expand_links(line, tokens))
        if hidden:
            self.notice(f"  … {hidden} more lines. Launch with --full to display complete entries.")
        displayed = "\n".join(lines)
        # Link artifacts outside the text preview exactly once, without another file list.
        for token, (path, label) in tokens.items():
            if token in content and token not in displayed:
                print("    File: " + self.hyperlink(path, label if self.links else str(path)))
        print(flush=True)


def main(argv=None):
    """Select a folder, replay bounded history, then follow new local records."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("repo", nargs="?", help="Project folder; omit for picker")
    parser.add_argument(
        "--history",
        type=int,
        default=50,
        help="Recent visible entries on startup (default 50; 0 for live only)",
    )
    parser.add_argument("--log-root", type=Path, help="Override Claude Code projects folder")
    parser.add_argument("--duration", type=float, help="Stop after N seconds (for verification)")
    parser.add_argument(
        "--full",
        action="store_true",
        help="Display complete entries without compacting long output",
    )
    parser.add_argument("--plain", action="store_true", help="Disable terminal colors")
    parser.add_argument(
        "--color",
        choices=("auto", "on", "off"),
        default="auto",
        help="Color preference; on overrides NO_COLOR in interactive terminals",
    )
    parser.add_argument(
        "--raw",
        action="store_true",
        help="Original detailed display with JSON and full session IDs",
    )
    parser.add_argument(
        "--max-lines",
        type=int,
        default=28,
        help="Maximum displayed lines per entry (default 28; --full overrides)",
    )
    parser.add_argument(
        "--links",
        choices=("auto", "on", "off"),
        default="auto",
        help="Clickable local file references in compatible terminals (default auto)",
    )
    parser.add_argument(
        "--details",
        action="store_true",
        help="Show expanded technical actions and output (default emphasizes human messages)",
    )
    parser.add_argument("--no-thinking", action="store_true", help="Hide saved thinking text")
    parser.add_argument("--no-prompts", action="store_true", help="Hide your typed prompts")
    args = parser.parse_args(argv)
    if args.history < 0 or (args.duration is not None and args.duration < 0):
        parser.error("history and duration must be non-negative")
    if args.max_lines < 1:
        parser.error("max-lines must be at least 1")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    claude_home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    root = args.log_root or claude_home / "projects"
    if not root.is_dir():
        parser.error(f"No local Claude Code projects directory: {root}")
    view = ConsoleView(
        args.full, args.plain, args.raw, args.max_lines, args.links, args.details, args.color
    )
    show = {"thinking": not args.no_thinking, "prompts": not args.no_prompts}
    catalog = Catalog([root])
    print(view.paint("\n  CLAUDEWATCH", "1;36"))
    view.notice("  Read-only activity feed  ·  Ctrl+C to stop")
    catalog.discover()
    given = (
        Path(os.path.expandvars(args.repo)).expanduser().absolute()
        if args.repo
        else pick_folder(catalog)
    )
    if given is None:
        return 0
    repo = given.resolve()
    if not repo.is_dir():
        parser.error(f"Folder does not exist: {repo}")
    # Sessions may record either spelling of a symlinked folder (for example /var on macOS).
    aliases = [repo] + ([given] if norm(given) != norm(repo) else [])
    folders = aliases + worktrees(repo)
    print(f"\n  Project  {clean(repo)}")
    view.notice("  Prompts · thinking · progress · actions · results · subagents · plans")
    view.notice("  Saved activity may lag the app. Redacted thinking is unavailable.")
    view.notice(
        "  File links: Ctrl+click a filename to open."
        if view.links
        else "  Local file paths are copyable. Use Windows Terminal for clickable filenames."
    )
    if not args.full and not args.raw:
        view.notice(
            f"  {'Detailed' if args.details else 'Focus'} view · messages up to {args.max_lines} lines · routine actions in one line"
        )
        view.notice("  --details expands technical output; --full displays complete entries.")
    tracked = set()
    backlog = []
    history_size = 0
    serial = 0
    matching = sorted(
        catalog.matching(folders), key=lambda log: (log.subagent, -log.path.stat().st_mtime)
    )
    print(f"\n  {len(matching)} saved sessions found (includes historical sessions).")
    for n, log in enumerate(matching):
        tracked.add(log.path)
        label = view.agent(log)
        if args.raw or n < 8:
            print(f"    {clean(log.label) if args.raw else label}")
        for record in log.records(initial=True, replay=args.history > 0):
            for event in visible(record, **show):
                if event.kind == "VIEWER NOTICE":
                    view.emit(log, record, event)
                    continue
                # Startup history has count AND text budgets; never keep full raw records.
                body = preview(event, 16384)
                context = {"timestamp": record.get("timestamp") or ""}
                if isinstance(record.get("toolDenialKind"), str):
                    context["toolDenialKind"] = record["toolDenialKind"]
                result = record.get("toolUseResult")
                if isinstance(result, dict):
                    context["toolUseResult"] = {
                        k: result[k]
                        for k in ("status", "interrupted", "returnCodeInterpretation")
                        if k in result
                    }
                serial += 1
                item = (context["timestamp"], serial, log, context, event._replace(body=body))
                heapq.heappush(backlog, item)
                history_size += len(body)
                while backlog and (len(backlog) > args.history or history_size > HISTORY_BYTES):
                    history_size -= len(heapq.heappop(backlog)[-1].body)
    if len(matching) > 8 and not args.raw:
        view.notice(
            f"    + {len(matching) - 8} other sessions; their labels appear when they have activity."
        )
    if args.history:
        view.notice(f"\n  RECENT HISTORY  ·  Last {args.history} entries")
        backlog.sort(key=lambda x: (x[0], x[1]))
        for _, _, log, record, event in backlog:
            view.emit(log, record, event)
    backlog.clear()
    print(
        view.paint(
            f"\n  LIVE WATCH  ·  {len(tracked)} saved sessions  ·  Waiting for new activity\n",
            "1;32",
        ),
        flush=True,
    )
    start = last_scan = last_heartbeat = last_worktrees = time.monotonic()
    while args.duration is None or time.monotonic() - start < args.duration:
        now = time.monotonic()
        if now - last_scan >= 4:
            catalog.discover()
            if now - last_worktrees >= 60:
                folders = aliases + worktrees(repo)
                last_worktrees = now
            tracked.intersection_update(catalog.logs)
            matching = catalog.matching(folders)
            last_scan = now
        had_events = False
        for log in matching:
            if log.path not in tracked:
                tracked.add(log.path)
                view.notice(f"\n  New matching agent/session: {view.agent(log)}")
            for record in log.records():
                for event in visible(record, **show):
                    had_events = True
                    view.emit(log, record, event)
        if had_events:
            last_heartbeat = now
        if now - last_heartbeat >= 30:
            view.notice(
                f"\n  Watching {len(tracked)} saved sessions · no new visible activity in 30 seconds"
            )
            last_heartbeat = now
        time.sleep(1)
    return 0


def cli():
    """Console entry point; Ctrl+C stops only the viewer."""
    try:
        return main()
    except KeyboardInterrupt:
        print("\nWatch stopped. Agents were not interrupted.")
        return 0


if __name__ == "__main__":
    raise SystemExit(cli())
