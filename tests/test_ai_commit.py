from __future__ import annotations

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
spec = importlib.util.spec_from_file_location("ai_commit", SOURCE / "ai_commit.py")
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)
import project_context
from investigation import StreamGuard, InvestigationLimit


@contextmanager
def cwd(path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


class CommitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ai-commit-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "repo with spaces"
        self.repo.mkdir()
        self.install = self.root / "installed helper"
        self.env = os.environ.copy()
        # Isolate every Git config mutation, including installer --global calls.
        self.env.update(GIT_CONFIG_GLOBAL=str(self.root / "global.gitconfig"),
                        GIT_CONFIG_NOSYSTEM="1", GIT_AUTHOR_NAME="Test",
                        GIT_AUTHOR_EMAIL="test@example.invalid",
                        GIT_COMMITTER_NAME="Test", GIT_COMMITTER_EMAIL="test@example.invalid")
        for key in ("GIT_INDEX_FILE", "GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR"):
            self.env.pop(key, None)
        self.git("init", "-q")
        self.git("config", "commit.gpgsign", "false")
        self.fake = self.root / ("fake-agy.cmd" if os.name == "nt" else "fake-agy")
        script = self.root / "fake.py"
        script.write_text('''import json, os, pathlib, subprocess, sys, time
if os.environ.get("FAKE_SLEEP"):
    time.sleep(120)
prompt = sys.stdin.read()
if os.environ.get("FAKE_PROMPT"):
    pathlib.Path(os.environ["FAKE_PROMPT"]).write_text(prompt, encoding="utf-8")
if os.environ.get("FAKE_MUTATE"):
    subprocess.run(["git", "-C", os.environ["FAKE_MUTATE"], "add", "later.txt"], check=True)
if os.environ.get("FAKE_BAD"):
    print("not json"); sys.exit(0)
if os.environ.get("FAKE_FAIL"):
    sys.exit(9)
if os.environ.get("FAKE_ERROR"):
    print(json.dumps({"event": "result", "result": {"status": "ERROR", "error": os.environ["FAKE_ERROR"]}}))
    sys.exit(1)
if os.environ.get("FAKE_CONTEXT_REPORT"):
    content = json.loads(prompt)["message"]["content"]
    metadata = json.loads(content.split("READ-ONLY INVESTIGATION CONTEXT:\\n", 1)[1])
    root = pathlib.Path(metadata["project_root"])
    report = {path.relative_to(root).as_posix(): path.read_text(encoding="utf-8") for path in root.rglob("*") if path.is_file()}
    pathlib.Path(os.environ["FAKE_CONTEXT_REPORT"]).write_text(json.dumps(report), encoding="utf-8")
if os.environ.get("FAKE_STREAM"):
    sys.stdout.write(os.environ["FAKE_STREAM"] + "\\n")
    sys.stdout.flush()
    time.sleep(120)
print(json.dumps({"event": "result", "result": {"status": "SUCCESS", "response": os.environ.get("FAKE_MESSAGE", "Add staged content")}}))
''', encoding="utf-8")
        if os.name == "nt":
            self.fake.write_text(f'@"{sys.executable}" "{script}"\n', encoding="utf-8")
        else:
            self.fake.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{script}"\n', encoding="utf-8")
            self.fake.chmod(0o755)

    def git(self, *args, check=True):
        return subprocess.run(["git", *args], cwd=self.repo, env=self.env,
                              capture_output=True, text=True, encoding="utf-8", check=check)

    def setup_install(self):
        result = subprocess.run([sys.executable, str(SOURCE / "install.py"),
                                 "--destination", str(self.install)],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        config = self.install / "config.json"
        data = json.loads(config.read_text())
        data["agy"] = str(self.fake)
        config.write_text(json.dumps(data), encoding="utf-8")

    def stage(self, name="file.txt", text="staged content\n"):
        (self.repo / name).write_text(text, encoding="utf-8")
        self.git("add", "--", name)

    def test_trigger_is_explicit(self):
        for value in ("AI", "ai", "Ai", "aI", " AI\n", "AI:", "AI1", "ai1", "aI1:"):
            self.assertEqual(helper.trigger(value), helper.GenerationRequest("low", ""))
        for token, effort in (("AI", "low"), ("AI1", "low"), ("AI2", "medium"), ("AI3", "high")):
            for casing in (token, token.lower(), token.replace("I", "i"), token.replace("A", "a")):
                self.assertEqual(helper.trigger(casing), helper.GenerationRequest(effort, ""))
                self.assertEqual(helper.trigger(casing + ": preserve selection"),
                                 helper.GenerationRequest(effort, "preserve selection"))
        for value in ("AIs", "AI integration", "Fix AI", "AI.\n", "AI\nother text", "AI0", "AI4",
                      "AI12", "AI2 fix this", "AI2\n: hint", "AI3: first\nsecond"):
            self.assertIsNone(helper.trigger(value))

    def test_invalid_messages_rejected(self):
        for value in (None, "", "AI", "ai1", "Ai2", "aI3", "```text\nA title\n```", "Here is your message:",
                      "A title\nNo blank line", "# Heading", "title\x1b[0m"):
            with self.subTest(value=value), self.assertRaises(helper.CommitError):
                helper.validate_message(value)
        self.assertEqual(helper.validate_message("Fix selection\n\nKeep the workspace ID."),
                         "Fix selection\n\nKeep the workspace ID.\n")

    def test_first_commit_only_includes_staged_content(self):
        self.setup_install()
        self.stage(text="STAGED_ONLY_SENTINEL\n")
        (self.repo / "file.txt").write_text("STAGED_ONLY_SENTINEL\nUNSTAGED_SENTINEL\n")
        (self.repo / "later.txt").write_text("UNTRACKED_SENTINEL\n")
        prompt = self.root / "prompt.json"
        self.env["FAKE_PROMPT"] = str(prompt)
        result = self.git("commit", "-m", "AI")
        self.assertIn("AI commit message", result.stderr)
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "Add staged content")
        self.assertEqual(self.git("show", "HEAD:file.txt").stdout, "STAGED_ONLY_SENTINEL\n")
        self.assertIn(" M file.txt", self.git("status", "--porcelain").stdout)
        self.assertIn("?? later.txt", self.git("status", "--porcelain").stdout)
        text = prompt.read_text(encoding="utf-8")
        self.assertIn("STAGED_ONLY_SENTINEL", text)
        self.assertNotIn("UNSTAGED_SENTINEL", text)
        self.assertNotIn("UNTRACKED_SENTINEL", text)

    def test_normal_commit_works_without_cli(self):
        self.setup_install()
        self.fake.unlink()
        self.stage()
        self.git("commit", "-m", "Keep my exact message")
        self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "Keep my exact message")

    def test_failure_retains_trigger_and_staging(self):
        self.setup_install()
        self.stage()
        self.env["FAKE_FAIL"] = "1"
        before = self.git("ls-files", "--stage").stdout
        result = self.git("commit", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AI commit stopped", result.stderr)
        self.assertEqual(self.git("ls-files", "--stage").stdout, before)
        self.assertEqual((self.repo / ".git/COMMIT_EDITMSG").read_text().strip(), "AI")
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_bad_json_stops_commit(self):
        self.setup_install()
        self.stage()
        self.env["FAKE_BAD"] = "1"
        result = self.git("commit", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("invalid JSON", result.stderr)

    def test_bad_message_stops_commit(self):
        self.setup_install()
        self.stage()
        self.env["FAKE_MESSAGE"] = "Here is your message:\n\nUpdate stuff"
        self.assertNotEqual(self.git("commit", "-m", "AI", check=False).returncode, 0)

    def test_concurrent_staging_stops_commit(self):
        self.setup_install()
        self.stage()
        (self.repo / "later.txt").write_text("later\n")
        self.env["FAKE_MUTATE"] = str(self.repo)
        result = self.git("commit", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("changed during generation", result.stderr)

    def test_existing_hook_failure_is_preserved(self):
        hook = self.repo / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho EXISTING_CHECK_FAILED >&2\nexit 7\n", newline="\n")
        hook.chmod(0o755)
        self.setup_install()
        self.stage()
        result = self.git("commit", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("EXISTING_CHECK_FAILED", result.stderr)

    def test_existing_prepare_and_commit_hooks_run(self):
        for name, line in (("prepare-commit-msg", "PREPARE_RAN"), ("commit-msg", "MESSAGE_RAN")):
            hook = self.repo / ".git/hooks" / name
            hook.write_text(f'#!/bin/sh\necho {line} >&2\n', newline="\n")
            hook.chmod(0o755)
        self.setup_install()
        self.stage()
        result = self.git("commit", "-m", "AI: explain the behavior")
        self.assertIn("PREPARE_RAN", result.stderr)
        self.assertIn("MESSAGE_RAN", result.stderr)

    def test_pre_push_preserves_stdin_arguments_and_exit(self):
        hook = self.repo / ".git/hooks/pre-push"
        hook.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\ncat\nexit 17\n', newline="\n")
        hook.chmod(0o755)
        self.setup_install()
        sh = str(Path(shutil.which("git")).parents[1] / "bin/sh.exe") if os.name == "nt" else "sh"
        result = subprocess.run([sh, str(self.install / "hooks/pre-push"), "origin", "remote-url"],
                                input="refs/heads/main abc refs/heads/main def\n",
                                cwd=self.repo, env=self.env, text=True, capture_output=True)
        self.assertEqual(result.returncode, 17)
        self.assertEqual(result.stdout, "origin\nremote-url\nrefs/heads/main abc refs/heads/main def\n")

    def test_prior_global_hooks_and_uninstall_are_preserved(self):
        old = self.root / "old hooks"
        old.mkdir()
        hook = old / "commit-msg"
        hook.write_text("#!/bin/sh\necho OLD_GLOBAL_HOOK >&2\n", newline="\n")
        hook.chmod(0o755)
        self.git("config", "--global", "core.hooksPath", old.as_posix())
        self.setup_install()
        self.stage()
        self.assertIn("OLD_GLOBAL_HOOK", self.git("commit", "-m", "AI").stderr)
        result = subprocess.run([sys.executable, str(self.install / "install.py"), "--uninstall",
                                 "--destination", str(self.install)], cwd=self.repo,
                                env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("config", "--global", "--get", "core.hooksPath").stdout.strip(),
                         old.as_posix())

    def run_setup(self, *args):
        return subprocess.run([sys.executable, str(self.install / "install.py"),
                               "--destination", str(self.install), *args], cwd=self.repo,
                              env=self.env, capture_output=True, text=True)

    def test_uninstall_is_repeatable_and_reinstall_preserves_original_settings(self):
        old = (self.root / "previous hooks").as_posix()
        self.git("config", "--global", "core.hooksPath", old)
        self.setup_install()
        for cycle in range(2):
            with self.subTest(cycle=cycle):
                removed = self.run_setup("--uninstall")
                self.assertEqual(removed.returncode, 0, removed.stderr)
                self.assertEqual(self.git("config", "--global", "--get", "core.hooksPath").stdout.strip(), old)
                again = self.run_setup("--uninstall")
                self.assertEqual(again.returncode, 0, again.stderr)
                self.assertIn("already disabled", again.stdout)
                restored = self.run_setup()
                self.assertEqual(restored.returncode, 0, restored.stderr)
                state = json.loads((self.install / "install-state.json").read_text())
                self.assertEqual(state["previous_global_hooks_paths"], [old])

    def test_uninstall_does_not_overwrite_a_new_hook_setting(self):
        self.setup_install()
        other = (self.root / "another tool").as_posix()
        self.git("config", "--global", "core.hooksPath", other)
        result = self.run_setup("--uninstall")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.git("config", "--global", "--get", "core.hooksPath").stdout.strip(), other)

    def test_failed_first_install_preserves_hooks_and_can_be_retried(self):
        old = (self.root / "original hooks").as_posix()
        self.git("config", "--global", "core.hooksPath", old)
        spec = importlib.util.spec_from_file_location("setup_module", SOURCE / "install.py")
        setup = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(setup)
        with cwd(self.repo), patch.dict(os.environ, self.env, clear=True), \
                patch.object(setup.shutil, "copy2", side_effect=OSError("Simulated copy failure")):
            with self.assertRaises(OSError):
                setup.install(self.install)
        self.assertEqual(self.git("config", "--global", "--get", "core.hooksPath").stdout.strip(), old)
        state = json.loads((self.install / "install-state.json").read_text())
        self.assertFalse(state["active"])
        self.setup_install()
        self.assertEqual(self.run_setup("--uninstall").returncode, 0)
        self.assertEqual(self.git("config", "--global", "--get", "core.hooksPath").stdout.strip(), old)

    def test_login_failure_is_actionable_and_preserves_staging(self):
        self.setup_install()
        self.stage()
        self.env["FAKE_ERROR"] = "AUTH_REQUIRED: authentication required"
        before = self.git("ls-files", "--stage").stdout
        result = self.git("commit", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("needs you to sign in", result.stderr)
        self.assertIn("run agy", result.stderr)
        self.assertEqual(self.git("ls-files", "--stage").stdout, before)
        self.assertEqual((self.repo / ".git/COMMIT_EDITMSG").read_text().strip(), "AI")
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_cli_error_categories_do_not_echo_sensitive_diagnostics(self):
        for diagnostic, expected in (("401 unauthorized", "sign in"),
                                     ("RESOURCE_EXHAUSTED quota", "quota or rate limit"),
                                     ("connection refused", "could not connect"),
                                     ("invalid model selection", "model or thinking level"),
                                     ("unexpected failure", "failed to generate")):
            with self.subTest(diagnostic=diagnostic):
                result = subprocess.CompletedProcess([], 1, b"", (diagnostic + " private-marker").encode())
                error = str(helper.cli_failure(result))
                self.assertIn(expected, error)
                self.assertNotIn("private-marker", error)

    def test_malformed_result_shapes_are_handled_without_a_traceback(self):
        for payload in (b"[]", b"null", b'{"event":"result","result":[]}',
                        b'{"event":"result"}', b'{"event":"result","result":null}'):
            with self.subTest(payload=payload), self.assertRaises(helper.CommitError):
                helper.read_envelope(payload)

    @unittest.skipUnless(os.name == "nt", "Windows launchers")
    def test_shared_zip_installs_and_uninstalls_with_double_click_launchers(self):
        spec = importlib.util.spec_from_file_location("build_release", SOURCE / "build_release.py")
        release = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(release)
        source_copy = self.root / "release source"
        source_copy.mkdir()
        for name in release.FILES:
            target = source_copy / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(SOURCE / name, target)
        (source_copy / "config.json").write_text('{"private":"must not ship"}')
        (source_copy / "install-state.json").write_text('{"private":"must not ship"}')
        with patch.object(release, "HERE", source_copy):
            release.main()
        download = self.root / "download with spaces"
        with zipfile.ZipFile(source_copy / "dist/ai-commit-windows.zip") as archive:
            self.assertNotIn("ai-commit/config.json", archive.namelist())
            self.assertNotIn("ai-commit/install-state.json", archive.namelist())
            archive.extractall(download)
        env = self.env | {"LOCALAPPDATA": str(self.root / "user app data")}
        for name in ("install.cmd", "uninstall.cmd", "install.cmd", "uninstall.cmd"):
            with self.subTest(launcher=name):
                result = subprocess.run([os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c",
                                         str(download / "ai-commit" / name)], env=env,
                                        cwd=self.repo, stdin=subprocess.DEVNULL,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.git("config", "--global", "--get", "core.hooksPath", check=False).stdout.strip())

    def test_large_diff_stops_without_cli_call(self):
        self.setup_install()
        self.stage(text="long line\n" * 30_000)
        self.fake.unlink()
        result = self.git("commit", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("larger than 240 KB", result.stderr)

    def test_amend_with_explicit_ai_message_is_rejected(self):
        self.setup_install()
        self.stage(text="original\n")
        self.git("commit", "-m", "Initial change")
        head = self.git("rev-parse", "HEAD").stdout
        self.stage(text="original\nmore\n")
        result = self.git("commit", "--amend", "-m", "AI", check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("disabled", result.stderr)
        self.assertEqual(self.git("rev-parse", "HEAD").stdout, head)

    def test_deletion_rename_and_binary_are_in_evidence(self):
        self.setup_install()
        self.stage("delete.txt", "delete this\n")
        self.stage("old.txt", "rename this\n")
        self.git("commit", "-m", "Initial files")
        self.git("rm", "delete.txt")
        self.git("mv", "old.txt", "new.txt")
        (self.repo / "asset.bin").write_bytes(b"\x00\xffbinary")
        self.git("add", "asset.bin")
        prompt = self.root / "evidence.json"
        self.env["FAKE_PROMPT"] = str(prompt)
        self.git("commit", "-m", "AI")
        text = prompt.read_text(encoding="utf-8")
        self.assertIn("deleted file mode", text)
        self.assertIn("rename from old.txt", text)
        self.assertIn("Binary files", text)

    def test_preview_does_not_commit_or_change_staging(self):
        self.setup_install()
        self.stage()
        before = self.git("ls-files", "--stage").stdout
        result = subprocess.run([sys.executable, str(self.install / "ai_commit.py"), "preview"],
                                cwd=self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "Add staged content\n")
        self.assertEqual(self.git("ls-files", "--stage").stdout, before)
        self.assertNotEqual(self.git("rev-parse", "--verify", "HEAD", check=False).returncode, 0)

    def test_no_staged_changes_stop(self):
        with cwd(self.repo), patch.dict(os.environ, self.env, clear=True):
            with self.assertRaisesRegex(helper.CommitError, "Nothing is staged"):
                helper.snapshot("")

    def test_cli_permissions_and_isolated_working_folder(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": "Fix selection"}}).encode(), b"")
        with patch.object(helper, "config", return_value={"agy": str(self.fake)}), \
                patch.object(helper, "invoke_cli", return_value=result) as mock:
            self.assertEqual(helper.run_cli("evidence"), "Fix selection\n")
        args, kwargs = mock.call_args
        argv, request, work = args
        settings = json.loads(argv[argv.index("--settings") + 1])
        self.assertEqual(settings["toolPermission"], "request-review")
        self.assertIn("command(*)", settings["permissions"]["deny"])
        self.assertIn("write_file(*)", settings["permissions"]["deny"])
        self.assertIn("mcp(*)", settings["permissions"]["deny"])
        self.assertNotIn("read_file(*)", settings["permissions"]["deny"])
        self.assertEqual(settings["permissions"]["allow"], [f"read_file({(Path(work) / 'context').as_posix()})"])
        self.assertIn("  - view_file", helper.AGENT)
        self.assertNotIn("  - run_command", helper.AGENT)
        self.assertNotIn("  - write_to_file", helper.AGENT)
        self.assertEqual(argv[argv.index("--model") + 1], "gemini-3.8-flash-low")
        self.assertEqual(argv[argv.index("--effort") + 1], "low")
        self.assertNotEqual(Path(work), self.repo)
        self.assertEqual(json.loads(request)["message"]["content"], "evidence")
        self.assertNotIn("evidence", argv)

    def test_each_thinking_level_pins_the_matching_flash_variant(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({"event": "result", "result": {
            "status": "SUCCESS", "response": "Fix selection"}}).encode(), b"")
        for effort in ("low", "medium", "high"):
            with self.subTest(effort=effort), \
                    patch.object(helper, "config", return_value={"agy": str(self.fake)}), \
                    patch.object(helper, "invoke_cli", return_value=result) as invoke:
                self.assertEqual(helper.run_cli("evidence", effort), "Fix selection\n")
                argv = invoke.call_args.args[0]
                self.assertEqual(argv[argv.index("--model") + 1], f"gemini-3.8-flash-{effort}")
                self.assertEqual(argv[argv.index("--effort") + 1], effort)

    def test_thinking_level_triggers_route_through_git_hook(self):
        self.setup_install()
        for number, (token, effort) in enumerate((("ai", "low"), ("Ai1", "low"),
                                                 ("aI2: explain the behavior", "medium"),
                                                 ("AI3", "high"))):
            with self.subTest(token=token):
                self.stage(text=f"change {number}\n")
                result = self.git("commit", "-m", token)
                self.assertIn(f"Gemini 3.8 Flash ({effort})", result.stderr)
                self.assertEqual(self.git("log", "-1", "--format=%s").stdout.strip(), "Add staged content")

    def test_invalid_effort_is_rejected_before_invocation(self):
        with self.assertRaises(helper.CommitError):
            helper.cli_args(str(self.fake), "unsupported")

    def test_cli_timeout_handles_unread_large_input(self):
        started = time.monotonic()
        with patch.dict(os.environ, {"FAKE_SLEEP": "1"}), patch.object(helper, "TIMEOUT", -14):
            with self.assertRaises(subprocess.TimeoutExpired):
                helper.invoke_cli([str(self.fake)], b"x" * 300_000, str(self.root))
        self.assertLess(time.monotonic() - started, 12)

    def test_project_context_uses_index_versions_and_excludes_unfinished_work(self):
        self.setup_install()
        original = "def describe():\n    return 'INDEX_VERSION'\n"
        self.stage("service.py", original)
        self.stage("README.md", "A small translation service.\n")
        self.git("commit", "-m", "Add service")
        staged = "from service import describe\nprint(describe())\n"
        self.stage("route.py", staged)
        (self.repo / "route.py").write_text(staged + "UNSTAGED_ROUTE = True\n")
        (self.repo / "service.py").write_text("UNSTAGED_SERVICE = True\n")
        (self.repo / "later.py").write_text("UNTRACKED_CONTEXT = True\n")
        report_path = self.root / "context-report.json"
        self.env["FAKE_CONTEXT_REPORT"] = str(report_path)
        self.git("commit", "-m", "AI")
        report = json.loads(report_path.read_text())
        self.assertEqual(report["service.py"], original)
        self.assertEqual(report["route.py"], staged)
        self.assertIn("README.md", report)
        self.assertNotIn("later.py", report)
        self.assertEqual((self.repo / "service.py").read_text(), "UNSTAGED_SERVICE = True\n")
        self.assertEqual((self.repo / "route.py").read_text(), staged + "UNSTAGED_ROUTE = True\n")

    def test_snapshot_never_materializes_symlinks_or_project_agent_instructions(self):
        index = (b"120000 " + b"a" * 40 + b" 0\toutside-link\0" +
                 b"100644 " + b"b" * 40 + b" 0\t.agents/hooks.json\0" +
                 b"100644 " + b"c" * 40 + b" 0\tAGENTS.md\0")
        with patch.object(helper, "git") as git_call:
            manifest = project_context.export_context(self.root / "context", index, (), git_call)
        git_call.assert_not_called()
        self.assertEqual(manifest["copied_files"], 0)
        self.assertEqual(manifest["omitted_files"], 3)

    def test_context_caps_prioritize_changed_code(self):
        self.stage("a.py", "unchanged = 1\n")
        self.stage("z.py", "changed = 1\n")
        with cwd(self.repo), patch.dict(os.environ, self.env, clear=True), \
                patch.object(project_context, "MAX_FILES", 1):
            index = helper.git("ls-files", "--stage", "-z").stdout
            manifest = project_context.export_context(self.root / "context", index, ("z.py",), helper.git)
        self.assertEqual([item["path"] for item in manifest["files"]], ["z.py"])
        self.assertEqual(manifest["omitted_files"], 1)
        if os.name == "nt":
            with self.assertRaises(PermissionError):
                (self.root / "context/project/z.py").write_text("should not write")

    def test_original_project_reads_and_all_mutations_are_denied(self):
        settings = helper.session_settings(str(self.root / "work"), str(self.repo))
        deny = settings["permissions"]["deny"]
        self.assertIn(f"read_file({self.repo.as_posix()})", deny)
        for action in ("write_file", "command", "read_url", "execute_url", "mcp"):
            self.assertIn(f"{action}(*)", deny)

    def test_running_cli_is_stopped_immediately_on_a_forbidden_tool(self):
        event = json.dumps({"event": "step_update", "step_update": {
            "step_index": 1, "step_type": "tool", "tool_name": "write_to_file", "state": "ACTIVE"}})
        started = time.monotonic()
        with patch.dict(os.environ, {"FAKE_STREAM": event}):
            with self.assertRaisesRegex(helper.CommitError, "outside its read-only scope"):
                helper.invoke_cli([str(self.fake)], b"{}", str(self.root))
        self.assertLess(time.monotonic() - started, 10)

    def test_running_cli_is_stopped_after_eight_investigation_calls(self):
        events = "\n".join(json.dumps({"event": "step_update", "step_update": {
            "step_index": index, "step_type": "tool", "tool_name": "grep_search", "state": "ACTIVE"}})
            for index in range(9))
        started = time.monotonic()
        with patch.dict(os.environ, {"FAKE_STREAM": events}):
            with self.assertRaisesRegex(helper.CommitError, "exceeded 8"):
                helper.invoke_cli([str(self.fake)], b"{}", str(self.root))
        self.assertLess(time.monotonic() - started, 10)


class InvestigationTests(unittest.TestCase):
    def event(self, index=1, name="view_file", state="DONE", output="some code"):
        return json.dumps({"event": "step_update", "step_update": {
            "conversation_id": "test", "step_index": index, "step_type": "tool",
            "state": state, "tool_name": name, "tool_info": {"output": output}}}).encode()

    def test_only_reading_and_search_are_allowed(self):
        for name in ("run_command", "write_to_file", "replace_file_content", "invoke_subagent", "mcp_any_tool"):
            with self.subTest(name=name), self.assertRaises(InvestigationLimit):
                StreamGuard().observe(self.event(name=name))

    def test_eight_calls_are_a_hard_limit_and_updates_are_deduplicated(self):
        guard = StreamGuard()
        for index in range(8):
            guard.observe(self.event(index, state="ACTIVE"))
            guard.observe(self.event(index))
            guard.observe(self.event(index))
        self.assertEqual(len(guard.calls), 8)
        with self.assertRaises(InvestigationLimit):
            guard.observe(self.event(8))

    def test_returned_context_bytes_are_bounded(self):
        with self.assertRaises(InvestigationLimit):
            StreamGuard().observe(self.event(output="x" * 128_001))

    def test_tool_paths_are_confined_to_the_snapshot(self):
        scope = Path(tempfile.gettempdir()) / "guard-test-context"
        event = json.loads(self.event())
        event["step_update"]["tool_info"]["parameters"] = {"AbsolutePath": str(scope / "project/file.py")}
        guard = StreamGuard(scope, scope.parent)
        guard.observe(json.dumps(event))
        event["step_update"]["tool_info"]["parameters"] = {"AbsolutePath": str(scope.parent / "outside.py")}
        with self.assertRaises(InvestigationLimit):
            guard.observe(json.dumps(event))

    def test_missing_tool_tracking_or_target_is_rejected(self):
        event = json.loads(self.event())
        event["step_update"].pop("step_index")
        with self.assertRaises(InvestigationLimit):
            StreamGuard().observe(json.dumps(event))
        with self.assertRaises(InvestigationLimit):
            StreamGuard(Path(tempfile.gettempdir()) / "snapshot").observe(self.event())

    def test_unsafe_snapshot_paths_are_rejected(self):
        for path in ("../outside", "/etc/passwd", "C:/data", "folder\\file", "CON.txt",
                     "folder/COM1", "file. ", ".env", "secrets.pem", ".agents/hooks.json", "GEMINI.md"):
            with self.subTest(path=path):
                self.assertFalse(project_context.safe_path(path))
        self.assertTrue(project_context.safe_path("src/service.py"))


if __name__ == "__main__":
    unittest.main()
