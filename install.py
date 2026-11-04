"""Install a shared AI commit hook, preserving the previous Git hook behavior."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

HERE = Path(__file__).resolve().parent
HOOKS = """applypatch-msg pre-applypatch post-applypatch pre-commit pre-merge-commit
prepare-commit-msg commit-msg post-commit pre-rebase post-checkout post-merge
pre-push pre-receive update proc-receive post-receive post-update
reference-transaction push-to-checkout pre-auto-gc post-rewrite sendemail-validate
fsmonitor-watchman p4-changelist p4-prepare-changelist p4-post-changelist
p4-pre-submit post-index-change""".split()


def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], check=check, capture_output=True,
                          text=True, encoding="utf-8")


def write(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def install(destination: Path) -> None:
    dest = destination.resolve()
    state_path = dest / "install-state.json"
    current = run("config", "--global", "--get-all", "core.hooksPath", check=False)
    previous_global = current.stdout.splitlines()
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8"))
        if not state.get("active", True) and previous_global != [(dest / "hooks").as_posix()]:
            # An explicit reinstall starts a new lifecycle and preserves whatever
            # hook settings the user currently has, including changes since removal.
            effective = run("-C", str(Path(sys.executable).parent), "config", "--get",
                            "core.hooksPath", check=False).stdout.strip()
            state = {"previous_global_hooks_paths": previous_global,
                     "previous_effective_hooks_path": effective}
        elif previous_global != [(dest / "hooks").as_posix()]:
            raise RuntimeError("Git's global hooks path changed since installation; "
                               "refusing to replace it. Uninstall/review that setting first.")
    else:
        if dest.exists() and any(dest.iterdir()):
            raise RuntimeError("The installation directory is not empty; refusing to overwrite it.")
        # Evaluate global/system config outside the user's current repository.
        effective = run("-C", str(Path(sys.executable).parent), "config", "--get",
                        "core.hooksPath", check=False).stdout.strip()
        state = {"previous_global_hooks_paths": previous_global,
                 "previous_effective_hooks_path": effective}
    git_exe = shutil.which("git")
    agy_exe = shutil.which("agy") or str(Path(os.environ.get("LOCALAPPDATA", "")) / "agy/bin/agy.exe")
    if not git_exe or not Path(agy_exe).is_file():
        raise RuntimeError("Git and Antigravity CLI must be installed before setup.")
    dest.mkdir(parents=True, exist_ok=True)
    if not state_path.exists():
        # Record recovery information before copying files, but activate Git last.
        # A failed first install can then be safely retried or uninstalled.
        state["active"] = False
        write(state_path, json.dumps(state, indent=2) + "\n")
    for name in ("ai_commit.py", "git_context.py", "process_tree.py", "project_context.py", "investigation.py",
                 "prompt.txt", "install.py", "README.md",
                 "install.cmd", "uninstall.cmd", "setup.cmd"):
        source = HERE / name
        if source.resolve() != (dest / name).resolve():
            shutil.copy2(source, dest / name)
    write(dest / "config.json", json.dumps({"git": git_exe, "agy": agy_exe}, indent=2) + "\n")
    hooks = dest / "hooks"
    hooks.mkdir(exist_ok=True)
    previous = state["previous_effective_hooks_path"]
    if previous:
        legacy = ('legacy=$(git -c core.hooksPath=' + shlex.quote(previous) +
                  ' rev-parse --git-path "hooks/$hook") || exit $?')
    else:
        legacy = ('common=$(git rev-parse --path-format=absolute --git-common-dir) || exit $?\n'
                  'legacy="$common/hooks/$hook"')
    dispatch = ("#!/bin/sh\n# AI Commit: preserve existing hooks and handle AI messages.\n"
                'hook="$1"\nshift\n' + legacy + '\n'
                'if test -x "$legacy"; then\n'
                '  "$legacy" "$@"\n  result=$?\n'
                '  test "$result" -eq 0 || exit "$result"\nfi\n'
                'if test "$hook" = prepare-commit-msg; then\n  exec ' +
                shlex.quote(Path(sys.executable).as_posix()) + " " +
                shlex.quote((dest / "ai_commit.py").as_posix()) + ' hook "$@"\nfi\nexit 0\n')
    write(dest / "dispatch.sh", dispatch)
    for name in HOOKS:
        target = hooks / name
        write(target, "#!/bin/sh\nexec sh " + shlex.quote((dest / "dispatch.sh").as_posix()) +
              " " + shlex.quote(name) + ' "$@"\n')
        target.chmod(0o755)
    # Activate only after every runtime file is present.
    run("config", "--global", "--replace-all", "core.hooksPath", hooks.as_posix())
    state["active"] = True
    write(state_path, json.dumps(state, indent=2) + "\n")
    print(f"Installed AI commit helper at {dest}")
    print("Stage changes, enter AI (or AI: a short hint), then Commit.")
    print("Gemini 3.8 Flash: AI/AI1 = low, AI2 = medium, AI3 = high (case insensitive).")
    print("Repositories with their own core.hooksPath override remain unchanged.")


def uninstall(destination: Path) -> None:
    dest = destination.resolve()
    current = run("config", "--global", "--get-all", "core.hooksPath", check=False).stdout.splitlines()
    state_path = dest / "install-state.json"
    if not state_path.exists():
        if current == [(dest / "hooks").as_posix()]:
            raise RuntimeError("The installation backup is missing. Git still points at this helper; "
                               "restore its backup before deleting files.")
        print("AI commit is not installed here. Git settings were left unchanged.")
        return
    state = json.loads(state_path.read_text(encoding="utf-8"))
    if current != [(dest / "hooks").as_posix()]:
        if not state.get("active", True) or current == state["previous_global_hooks_paths"]:
            state["active"] = False
            write(state_path, json.dumps(state, indent=2) + "\n")
            print("AI commit is already disabled. Git settings were left unchanged.")
            return
        raise RuntimeError("The global hooks path has changed; refusing to overwrite it.")
    previous = state["previous_global_hooks_paths"]
    if previous:
        run("config", "--global", "--replace-all", "core.hooksPath", previous[0])
    else:
        run("config", "--global", "--unset-all", "core.hooksPath")
    for value in previous[1:]:
        run("config", "--global", "--add", "core.hooksPath", value)
    state["active"] = False
    write(state_path, json.dumps(state, indent=2) + "\n")
    print("AI commit uninstalled. Previous global Git hook settings restored.")
    print(f"The inactive folder can now be deleted, or kept for reinstalling: {dest}")


def main() -> None:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=Path(
        os.environ.get("LOCALAPPDATA", str(Path.home() / ".local/share"))) / "AICommit")
    parser.add_argument("--uninstall", action="store_true")
    args = parser.parse_args()
    try:
        (uninstall if args.uninstall else install)(args.destination)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        parser.exit(1, f"Setup stopped: {exc}\n")


if __name__ == "__main__":
    main()
