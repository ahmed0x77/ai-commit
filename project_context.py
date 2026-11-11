"""Export staged patches and text context, never working-tree files."""
import json
from pathlib import Path, PurePosixPath
import re
import stat
import time

MAX_FILES = 2000
MAX_FILE_BYTES = 256_000
MAX_TOTAL_BYTES = 16_000_000
PREPARE_SECONDS = 8
PATCH_PAGE_BYTES = 12_000
PATCH_PAGE_LINES = 160
CONTROL_DIRS = {".git", ".agents", ".codex", ".gemini", ".claude", "node_modules", "vendor"}
CONTROL_FILES = {"agents.md", "gemini.md", "claude.md"}
SENSITIVE_SUFFIXES = {".pem", ".key", ".p12", ".pfx", ".jks"}


def diff_pages(data: bytes):
    """Split UTF-8 evidence without losing bytes, preferring line boundaries."""
    start = 0
    while start < len(data):
        end = min(start + PATCH_PAGE_BYTES, len(data))
        lines = data[start:end].split(b"\n", PATCH_PAGE_LINES)
        if len(lines) > PATCH_PAGE_LINES:
            end = start + sum(len(line) + 1 for line in lines[:PATCH_PAGE_LINES])
        elif end < len(data):
            newline = data.rfind(b"\n", start, end)
            if newline >= start:
                end = newline + 1
            else:
                while data[end] & 0xC0 == 0x80:
                    end -= 1
        yield data[start:end]
        start = end


def write_diff_pages(directory: Path, stem: str, extension: str, data: bytes) -> int:
    count = 0
    for count, page in enumerate(diff_pages(data), 1):
        path = directory / f"{stem}-{count}.{extension}"
        path.write_bytes(page)
        path.chmod(stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
    return count


def export_diff(directory: Path, diff: bytes, statistics: bytes, inline_bytes: int) -> dict:
    directory.mkdir()
    # Each top-level patch record starts here. A type change can produce two
    # records, so don't assume a one-to-one mapping with Git's numstat rows.
    boundaries = [match.start() for match in re.finditer(rb"(?m)^diff --git ", diff)]
    catalog = []
    starts = [0, *boundaries[1:]]
    for number, (start, end) in enumerate(zip(starts, starts[1:] + [len(diff)]), 1):
        patch = diff[start:end]
        stem = f"file-{number}"
        pages = write_diff_pages(directory, stem, "patch", patch)
        header = patch.partition(b"\n")[0].decode("utf-8")
        catalog.append(f"{stem}-1.patch ({pages} page{'s' if pages != 1 else ''})\t{header}")
    summary = {"staged_file_statistics": statistics.decode("utf-8", "replace"),
               "patch_catalog": "\n".join(catalog) + "\n"}
    if len(json.dumps(summary, ensure_ascii=False).encode("utf-8")) > inline_bytes:
        return {
            label: {"first_page": f"{label}-1.txt",
                    "page_count": write_diff_pages(directory, label, "txt", value.encode("utf-8"))}
            for label, value in summary.items()
        }
    return summary


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
