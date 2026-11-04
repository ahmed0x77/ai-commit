"""Bound and audit the read-only agent's streamed tool activity."""
import json
from pathlib import Path

READ_TOOLS = frozenset({"view_file", "grep_search", "list_dir", "find_by_name"})
MAX_TOOL_CALLS = 8
MAX_READ_BYTES = 128_000


class InvestigationLimit(Exception):
    pass


class StreamGuard:
    def __init__(self, scope=None, cwd=None):
        self.calls = set()
        self.completed = set()
        self.read_bytes = 0
        self.scope = Path(scope).resolve() if scope else None
        self.cwd = Path(cwd).resolve() if cwd else None

    def observe(self, line):
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            return  # The final result parser reports malformed output.
        if not isinstance(event, dict) or event.get("event") != "step_update":
            return
        step = event.get("step_update")
        if not isinstance(step, dict):
            return
        if step.get("subagent_info"):
            raise InvestigationLimit("The commit agent attempted delegation; the commit was stopped.")
        if step.get("step_type") != "tool":
            return
        info = step.get("tool_info") or {}
        if not isinstance(info, dict):
            raise InvestigationLimit("The CLI returned invalid tool activity; the commit was stopped.")
        name = step.get("tool_name") or info.get("name")
        if not isinstance(name, str) or name not in READ_TOOLS:
            raise InvestigationLimit("The commit agent attempted a tool outside its read-only scope.")
        parameters = info.get("parameters") or {}
        path_key = {"view_file": "AbsolutePath", "grep_search": "SearchPath",
                    "list_dir": "DirectoryPath", "find_by_name": "SearchDirectory"}[name]
        if not isinstance(parameters, dict):
            raise InvestigationLimit("The CLI returned invalid read-tool parameters.")
        if self.scope and step.get("state") == "DONE" and path_key not in parameters:
            raise InvestigationLimit("The CLI did not report the read target; the commit was stopped.")
        if self.scope and isinstance(parameters, dict) and path_key in parameters:
            raw_path = parameters[path_key]
            if not isinstance(raw_path, str):
                raise InvestigationLimit("The commit agent supplied an invalid context path.")
            path = Path(raw_path)
            if not path.is_absolute():
                path = (self.cwd or self.scope) / path
            if not path.resolve().is_relative_to(self.scope):
                raise InvestigationLimit("The commit agent attempted to read outside its snapshot.")
        index = step.get("step_index")
        conversation = step.get("conversation_id")
        if not isinstance(index, int) or index < 0 or not isinstance(conversation, (str, type(None))):
            raise InvestigationLimit("The CLI returned invalid tool tracking data.")
        key = (conversation, index)
        self.calls.add(key)
        if len(self.calls) > MAX_TOOL_CALLS:
            raise InvestigationLimit("Investigation exceeded 8 read/search calls. Retry with a focused hint.")
        if step.get("state") == "DONE" and key not in self.completed:
            self.completed.add(key)
            self.read_bytes += len(json.dumps(info.get("output", ""), ensure_ascii=False).encode("utf-8"))
            if self.read_bytes > MAX_READ_BYTES:
                raise InvestigationLimit("Investigation read too much context. Retry with a focused hint.")
