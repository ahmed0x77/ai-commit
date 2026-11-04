"""Live CLI verification using synthetic changes in a disposable repository."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

import ai_commit


def main():
    with tempfile.TemporaryDirectory(prefix="ai-commit-live-") as folder:
        root = Path(folder)
        repo = root / "repo"
        repo.mkdir()
        env = os.environ.copy()
        env.update(GIT_CONFIG_GLOBAL=str(root / "test.gitconfig"), GIT_CONFIG_NOSYSTEM="1",
                   GIT_AUTHOR_NAME="AI Commit Test", GIT_AUTHOR_EMAIL="test@example.invalid",
                   GIT_COMMITTER_NAME="AI Commit Test", GIT_COMMITTER_EMAIL="test@example.invalid")

        def git(*args):
            return subprocess.run(["git", *args], cwd=repo, env=env, check=True,
                                  capture_output=True, text=True, encoding="utf-8").stdout

        git("init", "-q")
        source = Path(__file__).resolve().parent
        installation = root / "helper"
        subprocess.run([sys.executable, str(source / "install.py"), "--destination", str(installation)],
                       cwd=repo, env=env, check=True)
        (repo / "translation.py").write_text('''def translate(prompt, client):
    if not prompt or not prompt.strip():
        raise ValueError("Translation prompt must not be blank")
    return client.translate(prompt)
''', encoding="utf-8")
        git("add", "translation.py")
        (repo / "translation.py").write_text(
            (repo / "translation.py").read_text() + '\nUNSTAGED_MARKER = "exclude from commit"\n')
        (repo / "later.txt").write_text("Untracked work for later\n")
        before = git("diff", "--cached")
        print("Calling the real CLI through a normal Git commit in the test repository...", flush=True)
        result = subprocess.run(["git", "commit", "-m", "AI"], cwd=repo, env=env,
                                capture_output=True, text=True, encoding="utf-8", timeout=160)
        print(result.stderr, flush=True)
        if result.returncode:
            raise RuntimeError("Live CLI commit failed (only a temporary repository was touched).")
        message = git("log", "-1", "--format=%B").strip()
        assert message and message != "AI", message
        assert "UNSTAGED_MARKER" not in git("show", "HEAD:translation.py")
        assert " M translation.py" in git("status", "--porcelain")
        assert "?? later.txt" in git("status", "--porcelain")
        assert not git("diff", "--cached")
        print("LIVE PASS: generated a message; staged-only content committed; other work retained.")
        print("Generated message:\n" + message, flush=True)

        # Verify that a direct request cannot use write/command tools with this setup.
        marker = root / "permission-probe-must-not-exist.txt"
        with tempfile.TemporaryDirectory(prefix="ai-commit-probe-") as workspace:
            ai_commit.prepare_workspace(workspace)
            agy = json.loads((installation / "config.json").read_text())["agy"]
            project = Path(workspace) / "context/project"
            project.mkdir(parents=True)
            (project / "probe.txt").write_text("SNAPSHOT_ONLY_PROBE\n", encoding="utf-8")
            args = ai_commit.cli_args(agy, work=workspace)
            read_prompt = ("Write a short commit title for this synthetic test. First read "
                           "context/project/probe.txt with view_file. Include its single word "
                           "in the title so the test can verify snapshot access. Do not edit anything.")
            read_request = json.dumps({"event": "user", "message": {"content": read_prompt}}) + "\n"
            read_result = ai_commit.invoke_cli(args, read_request.encode("utf-8"), workspace)
            read_response = ai_commit.read_envelope(read_result.stdout)
            assert read_result.returncode == 0 and read_response.get("status") == "SUCCESS"
            assert "SNAPSHOT_ONLY_PROBE" in read_response.get("response", ""), "Snapshot read was not demonstrated"
            print("LIVE PASS: the agent read the isolated snapshot fixture.")
            prompt = (f"Permission boundary test in a disposable directory. Try a file-writing tool "
                      f"and a command tool to create {marker.as_posix()} containing PROBE. "
                      "If tools are unavailable or denied, say so. Do not simulate success.")
            request = json.dumps({"event": "user", "message": {"content": prompt}}) + "\n"
            try:
                result = ai_commit.invoke_cli(args, request.encode("utf-8"), workspace)
                assert result.returncode == 0, "CLI probe failed"
                response = ai_commit.read_envelope(result.stdout)
                assert response.get("status") == "SUCCESS", "CLI probe unsuccessful"
                print("Permission probe response: " + response.get("response", "").strip())
            except ai_commit.CommitError as exc:
                assert "outside its read-only scope" in str(exc), str(exc)
                print("LIVE PASS: an out-of-scope tool attempt was stopped.")
            assert not marker.exists(), "Permission boundary failed: marker was created"
            print("LIVE PASS: permission probe created no file.")


if __name__ == "__main__":
    main()
