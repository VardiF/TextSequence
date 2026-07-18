from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Optional

from agents import Agent, Runner
from agents.items import ToolCallItem, ToolCallOutputItem
from agents.mcp import MCPServerStreamableHttp


AGENT_INSTRUCTIONS = """You are the TextSequence timeline editing agent.

Use only the TextSequence MCP tools provided by the connected local MCP server. Never use direct application functions, shell commands, filesystem tools, FFmpeg commands, or invented tool definitions.

Follow INSPECT -> RESOLVE -> VALIDATE -> MUTATE. Before every mutation inspect authoritative MCP state. For "this", "selected clip", and "here", call get_editor_context with the supplied editor session ID. For first/last/ordinal clips or gaps, call get_timeline and use its deterministic ordinals and stable IDs. Never guess IDs, revisions, tracks, or frame positions. Use the current revision returned by inspection for expected_revision.

For "Split this here", get context, get the current timeline, verify the playhead is strictly inside the selected clip, then call split_clip. For deletion or movement, resolve the target deterministically before mutating. For relative trim, use trim_clip with edge=start/end and a positive frames_to_remove value. Do not interpret "bad takes" or semantic video quality; explain that this capability is unavailable.

If a mutation returns STALE_REVISION, re-inspect context and timeline, resolve the original request again, and retry at most once only if the target remains unambiguous. Never loop or silently reinterpret the request. If context or target is ambiguous, ask a concise clarification question and do not mutate.

After a successful mutation, report the concrete action and resulting revision concisely. Do not reveal hidden reasoning, raw SDK traces, absolute media paths, API keys, or giant JSON blobs."""


class AgentConfigurationError(RuntimeError):
    pass


@dataclass
class AgentChatResult:
    message: str
    actions: list[dict[str, Any]]


class AgentRuntime:
    def __init__(self, mcp_url: str = "http://127.0.0.1:8000/mcp", runner: Any = None):
        self.mcp_url = mcp_url
        self._runner = runner or Runner
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._locks_guard = asyncio.Lock()
        self._history: dict[str, list[tuple[str, str]]] = {}

    @property
    def model(self) -> str:
        return os.getenv("TEXTSEQUENCE_OPENAI_MODEL", "gpt-5.6-luna")

    def configured(self) -> bool:
        return bool(os.getenv("OPENAI_API_KEY"))

    async def _session_lock(self, session_id: str) -> asyncio.Lock:
        async with self._locks_guard:
            return self._session_locks.setdefault(session_id, asyncio.Lock())

    async def run(self, session_id: str, message: str) -> AgentChatResult:
        if not self.configured():
            raise AgentConfigurationError("Built-in agent requires OPENAI_API_KEY")
        lock = await self._session_lock(session_id)
        async with lock:
            history = self._history.setdefault(session_id, [])[-6:]
            prompt = self._prompt(session_id, message, history)
            params = {"url": self.mcp_url}
            async with MCPServerStreamableHttp(params, cache_tools_list=True, name="TextSequence MCP") as server:
                agent = Agent(name="TextSequence Agent", model=self.model, instructions=AGENT_INSTRUCTIONS,
                              mcp_servers=[server])
                result = await self._runner.run(agent, prompt, max_turns=12)
            output = str(result.final_output or "")
            actions = self._action_log(getattr(result, "new_items", []))
            self._history[session_id] = (history + [(message, output)])[-8:]
            return AgentChatResult(output, actions)

    @staticmethod
    def _prompt(session_id: str, message: str, history: list[tuple[str, str]]) -> str:
        recent = "\n".join(f"User: {user}\nAgent: {assistant}" for user, assistant in history)
        return (f"Editor session ID: {session_id}\n"
                "A context snapshot was submitted immediately before this message. Use get_editor_context; do not trust chat history as canonical state.\n"
                f"Recent conversation (non-authoritative):\n{recent}\n\nUser request: {message}")

    @classmethod
    def _action_log(cls, items) -> list[dict[str, Any]]:
        actions = []
        for item in items:
            if isinstance(item, ToolCallItem):
                name, arguments = cls._tool_call(item.raw_item)
                actions.append({"tool": name, "summary": cls._call_summary(name, arguments), "arguments": cls._safe_args(arguments)})
            elif isinstance(item, ToolCallOutputItem) and actions:
                result = cls._safe_output(item.output)
                actions[-1]["summary"] = cls._result_summary(actions[-1]["tool"], actions[-1]["summary"], result)
        return actions

    @staticmethod
    def _tool_call(raw):
        if isinstance(raw, dict):
            name = raw.get("name", "unknown_tool")
            arguments = raw.get("arguments", {})
        else:
            name = getattr(raw, "name", "unknown_tool")
            arguments = getattr(raw, "arguments", {})
        if isinstance(arguments, str):
            try: arguments = json.loads(arguments)
            except json.JSONDecodeError: arguments = {}
        return name, arguments if isinstance(arguments, dict) else {}

    @staticmethod
    def _safe_args(arguments):
        blocked = {"path", "source_path", "command", "api_key"}
        return {key: value for key, value in arguments.items() if key not in blocked}

    @staticmethod
    def _safe_output(output):
        if isinstance(output, dict): data = output
        else:
            try: data = json.loads(str(output))
            except (TypeError, json.JSONDecodeError): return {}
        if not isinstance(data, dict): return {}
        result = {key: data[key] for key in ("ok", "revision", "project_id", "render_type") if key in data}
        if isinstance(data.get("error"), dict): result["error"] = {key: data["error"][key] for key in ("code", "current_revision") if key in data["error"]}
        return result

    @staticmethod
    def _call_summary(name, arguments):
        if name == "get_editor_context": return "Inspected editor context."
        if name == "get_timeline": return "Inspected authoritative timeline."
        return f"Called {name}."

    @staticmethod
    def _result_summary(name, fallback, result):
        if not result: return fallback
        if result.get("error"): return f"{fallback} Result: {result['error'].get('code', 'error')}."
        revision = result.get("revision")
        if name in {"split_clip", "delete_clip", "move_clip", "trim_clip", "render_preview", "export_project"} and revision is not None:
            return f"{fallback} Project is now revision {revision}."
        if name == "get_timeline" and revision is not None: return f"Inspected timeline revision {revision}."
        return fallback
