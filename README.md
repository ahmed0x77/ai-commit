# AI commit messages through Antigravity CLI

Stage the exact files or lines you want in your IDE. Enter `AI` in its commit
message box, then click **Commit**. Antigravity CLI examines the staged diff and
can read/search an isolated project snapshot to understand related code. It
replaces the trigger before Git creates the commit.
You can add context with `AI: focus on preserving the selected workspace`.
All triggers are case insensitive. Ordinary messages pass through unchanged.

| Message | Model | Thinking level |
| --- | --- | --- |
| `AI` or `AI1` | Gemini 3.8 Flash | Low |
| `AI2` | Gemini 3.8 Flash | Medium |
| `AI3` | Gemini 3.8 Flash | High |

For example, `ai2: focus on restoring the selected workspace` generates a medium
thinking message with that hint. `Ai`, `aI1`, `aI2`, and `ai3` work too. Each run
explicitly passes the matching `--model gemini-3.8-flash-<level>` and `--effort`
to Antigravity CLI. This overrides the model only for this invocation and does
not change your saved CLI model. If that model is unavailable, generation stops
instead of falling back to another model.

## Install and remove

Built for Windows 10/11. Requires Git for Windows, Python 3.10+, and a signed-in
Antigravity CLI (`agy`). No packages. Linux process handling is included but has
not been verified on a Linux machine; macOS process ancestry is not supported.

1. Install Git, Python 3.10 or newer, and Antigravity CLI if needed.
2. Open a terminal and run `agy` once to complete sign-in.
3. Extract the download and double-click **install.cmd**. No administrator access
   is normally needed. The setup window stays open so you can read the result.
4. Stage changes in your IDE, enter `AI`, and commit.

The installer checks that Git and the CLI executable exist; it does not confirm
your account, network, or model availability. Those are checked by the CLI when
you generate a message. To install from a terminal in the extracted folder:

```powershell
python .\install.py
```

This installs an independent copy in `%LOCALAPPDATA%\AICommit` and sets the
user's global `core.hooksPath`. It forwards Git's standard hooks to their previous
location, preserving arguments, stdin, and failure codes. Each repository's
existing `.git/hooks` is preserved when there was no previous hooks path.
Repositories with their own local `core.hooksPath` override the global path and
are not automatically enabled. Custom hooks installed by future Git versions
may need adding to the forwarding list in `install.py`.

To uninstall, double-click **uninstall.cmd** in the download or in
`%LOCALAPPDATA%\AICommit`. Or run:

```powershell
python "$env:LOCALAPPDATA\AICommit\install.py" --uninstall
```

Uninstall restores the previous global hook setting. It retains the installed
files as an inactive copy; you can delete that copy after setup reports success.
Running uninstall again is harmless. Running install again reinstalls or updates
the helper and preserves the hook settings that existed before that installation.
If another tool changes the hook setting while this helper is active, uninstall
stops rather than overwriting that tool's setting.

**Deleting the downloaded/source folder does not uninstall the helper.** Its
active copy is independent. Do not delete the installed folder before running
uninstall: Git would still point to the deleted hooks directory and its previous
hook behavior could be lost. Uninstall does not remove Python, Git, Antigravity,
your CLI login, CLI conversation records, or any project commits/files.

## What failures look like

The helper has no separate popup or login window. It prints an actionable error
to Git's output and returns failure. Your IDE may show its own failed-commit
notification; open its Git output/details panel to read the explanation.

| Failure reported by the CLI | Message and next action |
| --- | --- |
| Login missing or expired | Open a terminal, run `agy`, complete sign-in, then retry. |
| Quota or rate limit | Wait for reset, retry, or write a normal commit message. |
| Network / timeout | Check the connection or VPN, then retry or write a normal message. |
| Model unavailable | Check `agy models` or update the CLI; no substitute model is used. |
| Invalid / incomplete output | The commit stops; retry or write a normal message. |
| Investigation exceeds its limits | The commit stops; retry with a focused hint or write a normal message. |
| Duplicate agent tools | Update AI Commit and retry. |
| Unrecognized error | Git reports that the cause could not be identified and shows the CLI exit code when available. |

For example, a recognized authentication error appears as:

```text
AI commit stopped: Antigravity needs you to sign in. Open a terminal,
run agy, complete sign-in, then retry the commit. No commit was created.
```

Generation failures leave the trigger and staged selection intact. The helper
never silently commits `AI` as a fallback. It does not launch an interactive
login from the commit hook. A manually written message still works without the
model. The helper classifies errors from the CLI response, standard error, and
the final error in the invocation log. Git output uses fixed messages to keep
raw diagnostics private.

## Share a copy or publish the source

This folder can be the root of a standalone GitHub repository. Its commands use
relative paths and discover each person's Git/CLI executables at installation.
Each person signs into their own Antigravity account. There is no shared API key.
Do not publish the installed LocalAppData folder: its `config.json` and
`install-state.json` are specific to your machine. They are ignored here.

To make a shareable download, run this from the source folder:

```powershell
python .\build_release.py
```

Share `dist\ai-commit-windows.zip`. Its explicit file list includes the source,
launchers, instructions, and tests; it excludes installed settings, hook backups,
and caches. Recipients extract it and double-click **install.cmd**. A GitHub
source ZIP works the same way. Publishing and release uploads are separate
actions; this package builder only creates a local ZIP.

## What it does

- Reads the staged diff, including partial staging, new files, deleted files,
  renames, binary file notices, and the first commit in a new repository.
- Supplies eight recent subjects as style examples, never as changes to include.
- Uses the instructions and examples in `prompt.txt` for concise English titles
  and useful optional details. Existing Conventional Commit style is respected.
- Sends small staged diffs through stdin with their file statistics. When that
  evidence would exceed 24 KB as JSON, sends a catalog and file statistics
  instead, with read-only access to the complete patch in pages of up to 12 KB
  and 160 lines. An oversized catalog and its statistics are also paged.
  The initial prompt and any pages or context the agent reads are sent to the
  service behind your existing CLI login and may be retained in its local
  conversations.
- Runs a fresh CLI session in a temporary folder with a dedicated read-only
  agent. It supports `read_file` and the older `view_file`, `grep_search`,
  `list_dir`, and `find_by_name` tools when available. It can inspect
  related definitions, imports, callers, and tests. All file writes, commands,
  web operations, and MCP tools are explicitly denied. Original project reads
  are denied; context reads are granted inside the temporary snapshot. Other
  non-workspace reads require approval and are denied by the headless CLI.
  Saved settings and paid credit overage remain unchanged. The CLI still creates
  its own session metadata.
- The CLI may start its configured integrations during initialization even when
  model tool calls are denied. Temporary workspace MCP overrides are supplied,
  but the installed CLI was observed still starting global MCP processes. The
  helper never edits your saved MCP configuration. A Windows process job contains
  each invocation, and timeout/exit cleanup terminates its child processes.
- Exports text context directly from Git index blobs, including unchanged tracked
  code. It never copies working-tree versions. Symlinks, submodules, agent/hook
  instruction folders, credentials, binaries, and dependency trees are omitted.
  Omitted counts are explicit in the context manifest; the staged diff remains
  complete and authoritative even when some additional context is unavailable.
- Bounds snapshot preparation to an 8-second budget, 2,000 files, 256 KB per file,
  and 16 MB total. Changed files, project manifests, and nearby code have priority.
  The agent is instructed to use at most six focused calls, with 200 lines per
  read. A stream monitor stops it above eight read/search calls, 128 KB of tool
  output, an out-of-snapshot read, delegation, or any other tool type. Permission
  denials and the reader-only agent are the preventive controls; the stream
  monitor is an additional check, not a replacement for those controls.
- Stops on generation failure, timeout, invalid output, or changes to staging or
  HEAD during generation. It never auto-stages. It does not push.
- Supports a preview without committing, from a repository's terminal:

```powershell
python "$env:LOCALAPPDATA\AICommit\ai_commit.py" preview
python "$env:LOCALAPPDATA\AICommit\ai_commit.py" preview --effort medium
```

## Boundaries

Use **Commit / Commit Staged** after explicitly staging. IDE actions such as
**Commit All**, auto-staging settings, `git commit -a`, and path-specific commits
can change Git's index before the hook runs; the helper describes the snapshot
Git presents to it. It cannot recover an earlier selection after an IDE expands
it. Use your IDE's staged-only commit action when selection matters.

`AI` generates immediately before the commit, without a preview dialog. Results
are printed to Git's output and saved in the commit. IDE error panels expose
failure reasons. Push alone does not generate a message. Normal messages work
without the CLI or network. A client must execute standard Git hooks.

Automatic messages are deliberately disabled for merge/rebase/cherry-pick/revert
operations and amendments. Write a normal message in those cases. Large patches
use selective reading, so their messages may cover fewer details. Git commits
the entire staged selection regardless of the pages the model reads. Patches
containing non-UTF-8 text stop instead of silently dropping changes.
The CLI turn (investigation plus writing) is limited to 180 seconds, with a
195-second outer process deadline as backup. Snapshot preparation has its own
8-second budget. CLI startup, snapshot preparation, and cleanup add overhead;
these limits are not a promise of a fixed response time. Simple obvious changes
can be summarized without tool calls. Tests and commands are never run by the
agent. If TEMP is inside the original repository, the helper stops and requests
an external temporary folder instead of weakening the original-folder boundary.
The checks detect concurrent changes while generating; they do not lock out
external programs after the final check. Existing hooks remain responsible for
their normal checks and can still change/reject messages.

## Verification status on this machine

The local integration suite covers real Git operations in disposable repositories
with a fake CLI, including partial staging, unchanged-code context, exclusion of
unstaged versions, read-only tool policy, live stream limits, hook forwarding,
failed generation, invalid output, amend rejection, and timeout cleanup. The
latest live check stopped at its timeout without creating the synthetic commit.
Live CLI calls could not be verified here: credential-free requests to both
Google and the Antigravity API hosts timed out. This is a verification limitation,
not evidence that the user's IDE or CLI login is broken. A live verification can
be run from a normal terminal once network access is available:

```powershell
python .\smoke_test.py
```

The smoke test uses only synthetic files in a disposable repository. It generates
a real message, checks that unstaged work remains excluded, verifies a snapshot
read, and probes whether the agent refuses a file/command request. It does not change your
normal Git configuration or create commits in your projects.

## Verification

```powershell
python -m unittest discover -s .\tests -v
```

Tests use disposable repositories and a fake CLI. No user project is committed,
staged, or pushed by the tests. Live CLI smoke testing is separate.
