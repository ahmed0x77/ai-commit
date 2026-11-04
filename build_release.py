"""Build a source ZIP using an explicit allowlist, without local settings."""
from pathlib import Path
import zipfile

HERE = Path(__file__).resolve().parent
FILES = ("ai_commit.py", "git_context.py", "process_tree.py", "project_context.py", "investigation.py", "prompt.txt", "install.py",
         "install.cmd", "uninstall.cmd", "setup.cmd", "README.md", ".gitignore",
         "build_release.py", "smoke_test.py", "tests/test_ai_commit.py")


def main():
    destination = HERE / "dist" / "ai-commit-windows.zip"
    destination.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(HERE / name, "ai-commit/" + name)
    print(destination)


if __name__ == "__main__":
    main()
