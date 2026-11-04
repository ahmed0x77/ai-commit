"""Export bounded text context from Git index blobs, never working-tree files."""
import json
from pathlib import Path, PurePosixPath
import stat
import time

MAX_FILES = 2000
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 16_000_000
PREPARE_SECONDS = 8
CONTROL_DIRS = {".git", ".agents", ".codex", ".gemini", ".claude", "node_modules", "vendor"}
CONTROL_FILES = {"agents.md", "gemini.md", "claude.md"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}


def safe_path(name):
    path = PurePosixPath(name)
    if (not name or path.is_absolute() or ".." in path.parts or
            any(char in name for char in '\\:*?"<>|') or
            any(ord(char) < 32 for char in name) or
            any(part.endswith((".", " ")) for part in path.parts) or
            any(part.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *{f"COM{i}" for i in range(1, 10)},
                                               *{f"LPT{i}" for i in range(1, 10)}} for part in path.parts)):
        return False
    # Copy no hooks, agent instructions, credentials, or dependency trees.
    lower = path.name.lower()
    return not (any(part.lower() in CONTROL_DIRS for part in path.parts) or
                lower in CONTROL_FILES or lower.startswith(".env") or
                lower in {"credentials", ".git-credentials", "id_rsa", "id_ed25519"} or
                path.suffix.lower() in SENSITIVE_SUFFIXES)


def export_context(destination, index, changed_paths, git):
    """Return a manifest. Omitted context never changes the authoritative diff."""
    started = time.monotonic()
    context = Path(destination).resolve()
    project = context / "project"
    project.mkdir(parents=True)
    entries = []
    skipped = 0
    changed = set(changed_paths)
    parents = {str(PurePosixPath(name).parent) for name in changed}
    for record in index.split(b"\0"):
        if not record:
            continue
        metadata, raw_name = record.split(b"\t", 1)
        mode, oid, stage = metadata.split()
        try:
            name = raw_name.decode("utf-8")
        except UnicodeError:
            skipped += 1
            continue
        if mode not in (b"100644", b"100755") or stage != b"0" or not safe_path(name):
            skipped += 1
            continue
        entries.append((name, oid.decode("ascii")))

    def priority(entry):
        name = entry[0]
        basename = PurePosixPath(name).name.lower()
        return (0 if name in changed else
                1 if basename in {"readme.md", "package.json", "pyproject.toml", "cargo.toml", "go.mod"} else
                2 if str(PurePosixPath(name).parent) in parents else 3, name)

    entries.sort(key=priority)
    # A bounded candidate list also limits object metadata lookup work.
    candidates = entries[:MAX_FILES * 4]
    selected = []
    total = 0
    if candidates:
        ids = list(dict.fromkeys(oid for _, oid in candidates))
        checked = git("cat-file", "--batch-check", input_data=("\n".join(ids) + "\n").encode(),
                      timeout=max(0.1, PREPARE_SECONDS - (time.monotonic() - started))).stdout
        sizes = {}
        for line in checked.splitlines():
            oid, kind, size = line.split()
            if kind == b"blob":
                sizes[oid.decode()] = int(size)
        for name, oid in candidates:
            size = sizes.get(oid, MAX_FILE_BYTES + 1)
            if size > MAX_FILE_BYTES or total + size > MAX_TOTAL_BYTES:
                continue
            if len(selected) >= MAX_FILES:
                break
            selected.append((name, oid, size))
            total += size

    copied = []
    preparation_limited = False
    if selected:
        ids = list(dict.fromkeys(oid for _, oid, _ in selected))
        blobs = git("cat-file", "--batch", input_data=("\n".join(ids) + "\n").encode(),
                    timeout=max(0.1, PREPARE_SECONDS - (time.monotonic() - started))).stdout
        contents = {}
        offset = 0
        for expected_oid in ids:
            end = blobs.index(b"\n", offset)
            oid, kind, raw_size = blobs[offset:end].split()
            size = int(raw_size)
            if oid.decode() != expected_oid or kind != b"blob":
                raise ValueError("Git returned an unexpected context object")
            contents[expected_oid] = blobs[end + 1:end + 1 + size]
            offset = end + size + 2
        for name, oid, size in selected:
            if time.monotonic() - started >= PREPARE_SECONDS:
                preparation_limited = True
                break
            data = contents[oid]
            if b"\0" in data:
                continue
            try:
                data.decode("utf-8")
            except UnicodeError:
                continue
            target = project.joinpath(*PurePosixPath(name).parts)
            if not target.resolve().is_relative_to(project.resolve()):
                raise ValueError("Context path escaped the snapshot")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
            copied.append({"path": name, "bytes": size, "changed": name in changed})
    manifest = {"source": "Git index snapshot; never the live working tree",
                "files": copied, "copied_files": len(copied),
                "omitted_files": skipped + len(entries) - len(copied),
                "preparation_limited": preparation_limited,
                "context_only": "Unchanged files explain behavior, not extra commit changes."}
    manifest_path = context / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    manifest_path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return manifest
