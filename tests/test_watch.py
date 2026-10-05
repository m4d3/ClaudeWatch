import importlib.util
import json
from pathlib import Path
import tempfile
import subprocess
import sys
import threading
import unittest
import contextlib
import io
import ctypes
from unittest import mock

spec = importlib.util.spec_from_file_location("watch", Path(__file__).parents[1] / "claudewatch.py")
w = importlib.util.module_from_spec(spec)
spec.loader.exec_module(w)


def row(kind, **fields):
    return {"type": kind, "timestamp": "2026-10-03T14:00:00Z", **fields}


def line(kind, **fields):
    return (json.dumps(row(kind, **fields), ensure_ascii=False) + "\n").encode("utf-8")


def assistant(*blocks, stop=None, **fields):
    return line(
        "assistant",
        message={"role": "assistant", "content": list(blocks), "stop_reason": stop},
        **fields,
    )


def tool_result(call, content, is_error=False, **fields):
    block = {"type": "tool_result", "tool_use_id": call, "content": content, "is_error": is_error}
    return line("user", message={"role": "user", "content": [block]}, **fields)


def render(view, log, raw_line):
    record = json.loads(raw_line)
    with contextlib.redirect_stdout(io.StringIO()) as output:
        for event in w.visible(record):
            view.emit(log, record, event)
    return output.getvalue()


def session(root, project, sid, cwd, *records):
    folder = root / project
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{sid}.jsonl"
    path.write_bytes(
        line("queue-operation", sessionId=sid)
        + line("user", cwd=str(cwd), sessionId=sid, message={"role": "user", "content": "hi"})
        + b"".join(records)
    )
    return path


def subagent(root, project, sid, aid, cwd, meta=None, *records):
    folder = root / project / sid / "subagents"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"agent-{aid}.jsonl"
    first = line(
        "user",
        cwd=str(cwd),
        sessionId=sid,
        agentId=aid,
        isSidechain=True,
        parentUuid=None,
        message={"role": "user", "content": "TASK_PROMPT_SHOWN_BY_PARENT"},
    )
    path.write_bytes(first + b"".join(records))
    if meta:
        (folder / f"agent-{aid}.meta.json").write_text(json.dumps(meta))
    return path


class WatchTests(unittest.TestCase):
    def test_assistant_blocks_and_answer_phase(self):
        record = json.loads(
            assistant(
                {"type": "thinking", "thinking": "Saved thinking", "signature": "SIGNATURE"},
                {"type": "redacted_thinking", "data": "OPAQUE"},
                {"type": "text", "text": "Done."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls"}},
                stop="end_turn",
            )
        )
        events = w.visible(record)
        self.assertEqual([e.kind for e in events], ["THINKING", "MESSAGE answer", "ACTION"])
        self.assertEqual(events[2].name, "Bash")
        self.assertEqual(events[2].call, "t1")
        self.assertNotIn("SIGNATURE", repr(events))
        self.assertNotIn("OPAQUE", repr(events))
        self.assertEqual([e.kind for e in w.visible(record, thinking=False)][0], "MESSAGE answer")
        progress = json.loads(assistant({"type": "text", "text": "Checking"}, stop="tool_use"))
        self.assertEqual(w.visible(progress)[0].kind, "MESSAGE progress")

    def test_system_context_is_never_displayed(self):
        hidden = (
            row("user", isMeta=True, message={"role": "user", "content": "PRIVATE"}),
            row(
                "user",
                message={"role": "user", "content": "<system-reminder>PRIVATE</system-reminder>"},
            ),
            row(
                "user",
                message={
                    "role": "user",
                    "content": "<local-command-stdout>PRIVATE</local-command-stdout>",
                },
            ),
            row("user", isCompactSummary=True, message={"role": "user", "content": "PRIVATE"}),
            row("attachment", attachment={"type": "instructions", "content": "PRIVATE"}),
            row("system", subtype="stop_hook_summary", content="PRIVATE"),
            row("file-history-snapshot", snapshot={"PRIVATE": 1}),
            row("bridge-session", ownerAccountUuid="PRIVATE"),
        )
        for record in hidden:
            with self.subTest(record=record["type"]):
                self.assertEqual(w.visible(record), [])
        mixed = row(
            "user",
            message={
                "role": "user",
                "content": "<system-reminder>PRIVATE</system-reminder>\nFix the build",
            },
        )
        self.assertEqual(w.visible(mixed), [w.Event("PROMPT", "Fix the build")])
        self.assertEqual(w.visible(mixed, prompts=False), [])
        command = row(
            "user",
            message={
                "role": "user",
                "content": "<command-name>/review</command-name><command-args>high</command-args>",
            },
        )
        self.assertEqual(w.visible(command)[0].body, "/review high")

    def test_result_severity(self):
        log = w.Log(Path("p/s.jsonl"), {"session": "s"})
        cases = (
            ("All good", False, {}, None),
            ("Exit code 1\nPermission denied", True, {}, "ERROR"),
            ("Exit code 2\nTraceback (most recent call last):", True, {}, "ERROR"),
            ("<tool_use_error>File has not been read yet.</tool_use_error>", True, {}, "ATTENTION"),
            ("PreToolUse:Bash hook error: blocked", True, {}, "ATTENTION"),
            ("Permission denied by rule", True, {"toolDenialKind": "permission-rule"}, "ATTENTION"),
            ("partial", False, {"toolUseResult": {"interrupted": True}}, "ATTENTION"),
            ("PASS: 0 errors", False, {}, None),
        )
        for body, is_error, fields, severity in cases:
            with self.subTest(body=body):
                view = w.ConsoleView()
                view.color = True
                rendered = render(view, log, tool_result("c", body, is_error, **fields))
                if severity:
                    self.assertIn(severity + ":", rendered)
                else:
                    self.assertNotIn("ATTENTION:", rendered)
                    self.assertNotIn("ERROR:", rendered)
                self.assertEqual("\x1b[1;91m" in rendered, severity == "ERROR")
                self.assertNotIn("tool_use_error", rendered)
        view = w.ConsoleView(plain=True)
        rendered = render(view, log, tool_result("c", "x", True, toolDenialKind="user-rejected"))
        self.assertIn("Rejected by the user", rendered)

    def test_explicit_color_overrides_inherited_no_color_but_not_plain_or_redirection(self):
        with (
            mock.patch.dict(w.os.environ, {"NO_COLOR": "1"}),
            mock.patch.object(w.os, "name", "posix"),
            mock.patch.object(sys.stdout, "isatty", return_value=True),
        ):
            self.assertFalse(w.ConsoleView().color)
            self.assertTrue(w.ConsoleView(color_mode="on").color)
            self.assertFalse(w.ConsoleView(color_mode="off").color)
            self.assertFalse(w.ConsoleView(plain=True, color_mode="on").color)
            with mock.patch.object(sys.stdout, "isatty", return_value=False):
                self.assertFalse(w.ConsoleView(color_mode="on").color)

    def test_windows_color_setup_uses_pointer_sized_handles(self):
        kernel = mock.MagicMock()
        kernel.GetStdHandle.return_value = 0x123456789
        kernel.GetConsoleMode.return_value = 1
        kernel.SetConsoleMode.return_value = 1
        with (
            mock.patch.object(sys.stdout, "isatty", return_value=True),
            mock.patch.dict(w.os.environ, {}, clear=True),
            mock.patch.object(w.os, "name", "nt"),
            mock.patch.object(ctypes, "windll", mock.Mock(kernel32=kernel), create=True),
        ):
            view = w.ConsoleView()
        self.assertTrue(view.color)
        self.assertEqual(kernel.GetConsoleMode.argtypes[0], ctypes.c_void_p)
        self.assertEqual(kernel.GetConsoleMode.call_args.args[0], 0x123456789)

    def test_focus_view_compacts_tools_and_keeps_conversation(self):
        log = w.Log(Path("p/s.jsonl"), {"session": "s"})
        view = w.ConsoleView(plain=True)
        action = assistant(
            {
                "type": "tool_use",
                "id": "b1",
                "name": "Bash",
                "input": {"command": "npm test", "description": "Run the tests"},
            }
        )
        self.assertEqual(render(view, log, action).strip().count("\n"), 0)
        self.assertIn(
            "Action: Bash · Run the tests", render(view, log, action.replace(b"b1", b"b2"))
        )
        result = render(view, log, tool_result("b2", "\n".join(["ok"] * 500)))
        self.assertIn("Result: Bash · 500 lines", result)
        self.assertEqual(result.strip().count("\n"), 0)
        edit = assistant(
            {"type": "tool_use", "id": "e1", "name": "Edit", "input": {"file_path": "/x/app.py"}}
        )
        self.assertIn("Files: Edit · /x/app.py", render(view, log, edit))
        mcp = assistant(
            {"type": "tool_use", "id": "m1", "name": "mcp__github__create_issue", "input": {}}
        )
        self.assertIn("Action: github · create issue", render(view, log, mcp))
        message = assistant(
            {"type": "text", "text": "I found the cause.\nThe shader uses the wrong normal."}
        )
        shown = render(view, log, message)
        self.assertIn("Progress", shown)
        self.assertIn("The shader uses the wrong normal.", shown)

    def test_coordination_becomes_readable_messages(self):
        log = w.Log(Path("p/s.jsonl"), {"session": "s"})
        view = w.ConsoleView(plain=True)
        spawn = assistant(
            {
                "type": "tool_use",
                "id": "a1",
                "name": "Agent",
                "input": {
                    "description": "Review shaders",
                    "subagent_type": "Explore",
                    "prompt": "Please check the light direction.",
                    "run_in_background": True,
                },
            }
        )
        shown = render(view, log, spawn)
        self.assertIn("Agent message", shown)
        self.assertIn("To: Explore · Review shaders · background", shown)
        self.assertIn("Please check the light direction.", shown)
        launched = tool_result(
            "a1", "Async agent launched", toolUseResult={"status": "async_launched"}
        )
        self.assertIn("launched in background", render(view, log, launched))
        render(view, log, spawn.replace(b'"a1"', b'"a2"'))
        report = tool_result(
            "a2",
            [{"type": "text", "text": "REPORT: the normal is flipped."}],
            toolUseResult={"status": "completed"},
        )
        self.assertIn("REPORT: the normal is flipped.", render(view, log, report))
        todo = assistant(
            {
                "type": "tool_use",
                "id": "t1",
                "name": "TodoWrite",
                "input": {
                    "todos": [
                        {"content": "Fix normal", "status": "completed"},
                        {"content": "Rebuild", "status": "in_progress"},
                    ]
                },
            }
        )
        shown = render(view, log, todo)
        self.assertIn("Plan", shown)
        self.assertIn("[x] Fix normal", shown)
        self.assertIn("[>] Rebuild", shown)
        self.assertEqual(render(view, log, tool_result("t1", "Todos modified")), "")
        note = line(
            "user",
            origin={"kind": "task-notification"},
            message={
                "role": "user",
                "content": "<task-notification><task-id>x</task-id><status>failed</status>"
                '<summary>Agent "Review" failed</summary><result>PRIVATE</result>'
                "</task-notification>",
            },
        )
        shown = render(view, log, note)
        self.assertIn("ERROR", shown)
        self.assertIn('Agent "Review" failed', shown)
        self.assertNotIn("PRIVATE", shown)

    def test_sessions_subagents_and_worktree_matching(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "projects"
            repo = Path(folder) / "repo"
            repo.mkdir()
            key = w.project_key(repo)
            session(root, key, "main", repo)
            subagent(
                root,
                key,
                "main",
                "a1",
                Path(folder) / "elsewhere",
                {"agentType": "code-reviewer", "description": "Review"},
            )
            session(root, "other-project", "other", Path(folder) / "repo-other")
            catalog = w.Catalog([root])
            catalog.discover()
            matched = {log.id: log for log in catalog.matching([repo])}
            self.assertEqual(set(matched), {"main", "a1"})
            view = w.ConsoleView(plain=True)
            self.assertIn("Code reviewer (Review)", view.agent(matched["a1"]))
            self.assertIn("Main session", view.agent(matched["main"]))
            session(root, "wt-project", "wt", Path(folder) / "wt")
            # A brand-new session without a recorded folder still matches its project directory.
            (root / key / "fresh.jsonl").write_bytes(line("queue-operation", sessionId="fresh"))
            catalog.discover()
            self.assertEqual(
                {x.id for x in catalog.matching([repo, Path(folder) / "wt"])},
                {"main", "a1", "wt", "fresh"},
            )
            self.assertEqual(w.project_key(r"C:\Projects\My_App"), "C--Projects-My-App")

    def test_partial_utf8_then_append_and_truncation(self):
        with tempfile.TemporaryDirectory() as folder:
            p = Path(folder) / "live.jsonl"
            p.write_bytes(line("queue-operation"))
            log = w.Log(p, {"session": "one"})
            self.assertEqual(list(log.records(initial=True)), [])
            message = assistant({"type": "text", "text": "Snow \u2603"})
            cut = message.index("\u2603".encode("utf-8")) + 1
            with p.open("ab") as f:
                f.write(message[:cut])
            self.assertEqual(list(log.records()), [])
            with p.open("ab") as f:
                f.write(message[cut:])
            self.assertEqual(w.visible(list(log.records())[0])[0].body, "Snow \u2603")
            self.assertEqual(list(log.records()), [])
            p.write_bytes(line("system", subtype="compact_boundary"))
            self.assertEqual(w.visible(list(log.records())[0])[0].kind, "STATUS")

    def test_oversized_records_do_not_block_next_record(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "test.jsonl"
            path.write_bytes(line("queue-operation"))
            log = w.Log(path, {"session": "a"})
            list(log.records(initial=True))
            with path.open("ab") as stream:
                stream.write(b"x" * 256 + b"\n" + line("system", subtype="compact_boundary"))
            with mock.patch.object(w, "MAX_RECORD_BYTES", 128):
                records = list(log.records())
            self.assertEqual(records[0]["type"], "viewer_notice")
            self.assertEqual(records[-1]["subtype"], "compact_boundary")

    def test_terminal_sanitization_in_all_display_modes(self):
        log = w.Log(Path("p/s.jsonl"), {"session": "s"})
        stamp = "\x1b[2J\x1b]0;untrusted title\x07invalid"
        for options in ({}, {"details": True}, {"raw": True}):
            with self.subTest(options=options):
                view = w.ConsoleView(plain=True, **options)
                event = w.Event("MESSAGE progress", "SAFE\x1b[31m_MESSAGE")
                with contextlib.redirect_stdout(io.StringIO()) as output:
                    view.emit(log, {"timestamp": stamp}, event)
                shown = output.getvalue()
                self.assertIn("SAFE_MESSAGE", shown)
                self.assertIn("invalid", shown)
                self.assertNotIn("\x1b", shown)
                self.assertNotIn("untrusted title", shown)

    def test_file_links_and_plain_output(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            image = repo / "snow scene ü.png"
            image.write_bytes(b"fixture")
            (repo / "shader.py").write_text("# fixture")
            text = f"[Preview](<{image}:12>)\n`shader.py`\nmissing.png\nhttps://example.com/a.png"
            self.assertEqual(
                set(w.file_references(text, str(repo))),
                {image.resolve(), (repo / "shader.py").resolve()},
            )
            with mock.patch.object(sys.stdout, "isatty", return_value=True):
                view = w.ConsoleView(links="on")
            log = w.Log(Path("p/s.jsonl"), {"session": "s", "cwd": str(repo)})
            shown = render(view, log, assistant({"type": "text", "text": f"See `{image}`"}))
            self.assertIn(image.as_uri(), shown)
            with mock.patch.object(sys.stdout, "isatty", return_value=False):
                self.assertEqual(w.ConsoleView(links="on").hyperlink(image, "x"), "x")

    def test_history_preview_keeps_tool_arguments_parseable(self):
        event = w.Event(
            "ACTION", json.dumps({"file_path": "/x/big.txt", "content": "y" * 50000}), "Write"
        )
        shortened = w.preview(event, 16384)
        self.assertLessEqual(len(shortened), 16384)
        self.assertEqual(json.loads(shortened)["file_path"], "/x/big.txt")

    def test_malformed_records_are_skipped(self):
        bad = (
            row("assistant", message="not a dict"),
            row("assistant", timestamp=[], message={"content": []}),
            row("assistant", message={"content": ["not a block"]}),
            row("user", message={"content": [{"type": "tool_result", "content": "\ud800"}]}),
            None,
        )
        for record in bad:
            with self.subTest(record=record):
                self.assertEqual(w.visible(record), [w.Event("VIEWER NOTICE", w.MALFORMED_NOTICE)])
        log = w.Log(Path("p/s.jsonl"), {"session": "s"})
        view = w.ConsoleView(plain=True)
        with contextlib.redirect_stdout(io.StringIO()) as output:
            view.emit(log, {}, w.Event("ACTION", '{"questions": 5}', "AskUserQuestion"))
            view.emit(log, {}, w.Event("MESSAGE progress", "FOLLOWING_MESSAGE"))
        self.assertIn("FOLLOWING_MESSAGE", output.getvalue())

    def test_history_and_raw_modes_end_to_end(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "projects"
            repo = Path(folder) / "repo"
            repo.mkdir()
            session(
                root,
                w.project_key(repo),
                "main",
                repo,
                b"{invalid json}\nnull\n",
                assistant({"type": "text", "text": "HISTORY_MESSAGE"}, stop="end_turn"),
            )
            for options in ([], ["--details"], ["--raw"]):
                with self.subTest(options=options):
                    with contextlib.redirect_stdout(io.StringIO()) as output:
                        result = w.main(
                            [str(repo), "--log-root", str(root), "--duration", "0", "--plain"]
                            + options
                        )
                    self.assertEqual(result, 0)
                    self.assertIn(w.MALFORMED_NOTICE, output.getvalue())
                    self.assertIn("HISTORY_MESSAGE", output.getvalue())

    def test_end_to_end_live_feed_discovers_new_subagent(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder) / "projects"
            repo = Path(folder) / "repo"
            repo.mkdir()
            key = w.project_key(repo)
            main = session(root, key, "main", repo)

            def append():
                with main.open("ab") as f:
                    f.write(b"{invalid json}\n")
                    f.write(assistant({"type": "text", "text": "LIVE_OUTPUT_MARKER"}))
                subagent(
                    root,
                    key,
                    "main",
                    "child",
                    Path(folder) / "other",
                    {"agentType": "test-child"},
                    assistant(
                        {"type": "thinking", "thinking": "CHILD_THINKING", "signature": "SECRET"}
                    ),
                )

            timer = threading.Timer(1.5, append)
            timer.start()
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(Path(w.__file__)),
                        str(repo),
                        "--log-root",
                        str(root),
                        "--history",
                        "0",
                        "--duration",
                        "6",
                    ],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    timeout=12,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("LIVE_OUTPUT_MARKER", result.stdout)
                self.assertIn(w.MALFORMED_NOTICE, result.stdout)
                self.assertIn("CHILD_THINKING", result.stdout)
                self.assertIn("Test child", result.stdout)
                self.assertNotIn("SECRET", result.stdout)
                self.assertNotIn("TASK_PROMPT_SHOWN_BY_PARENT", result.stdout)
                self.assertIn("New matching agent/session", result.stdout)
            finally:
                timer.join()

    def test_git_lookup_ignores_relative_path_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            name = "git.exe" if w.os.name == "nt" else "git"
            planted = Path(folder) / name
            planted.write_bytes(b"")
            planted.chmod(0o755)
            with (
                contextlib.chdir(folder),
                mock.patch.dict(w.os.environ, {"PATH": "." + w.os.pathsep + "relative"}),
            ):
                self.assertIsNone(w.find_git())
                self.assertEqual(w.worktrees(Path(folder)), [])
            with mock.patch.dict(w.os.environ, {"PATH": folder}):
                self.assertEqual(w.find_git(), str(planted))

    def test_worktrees_lists_registered_checkouts(self):
        if not w.find_git():
            self.skipTest("Git is not installed")
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder) / "repo"
            repo.mkdir()
            subprocess.run([w.find_git(), "init", "-q", str(repo)], check=True)
            found = [w.norm(path) for path in w.worktrees(repo)]
            self.assertIn(w.norm(repo.resolve()), found)

    def test_caches_are_bounded_and_private_metadata_is_not_retained(self):
        log = w.Log(Path("p/s.jsonl"), {"session": "s", "ownerAccountUuid": "PRIVATE"})
        self.assertNotIn("ownerAccountUuid", log.meta)
        with mock.patch.object(w, "MAX_SESSIONS", 2):
            view = w.ConsoleView(plain=True)
            for n in range(3):
                view.agent(w.Log(Path(f"p/{n}.jsonl"), {"session": str(n)}))
            self.assertEqual(len(view.tags), 2)
            self.assertEqual(len(view.names), 2)


if __name__ == "__main__":
    unittest.main()
