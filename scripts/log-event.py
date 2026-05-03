#!/usr/bin/env python3
"""Logfire trace exporter for Cursor IDE.

Ported from pydantic/claude-code-logfire-plugin.
Captures session traces via Cursor's hooks system and sends to Logfire as OTel spans.

Trace hierarchy:
  Cursor session (root span)
  +-- chat model-name       <- LLM API call 1
  +-- chat model-name       <- LLM API call 2
  ...

Configuration (in priority order):
  1. LOGFIRE_TOKEN env var
  2. Windows registry HKCU\\Environment\\LOGFIRE_TOKEN (User-scope env, used as
     fallback because Cursor on Windows does not always inherit User-scope env
     vars into hook subprocesses spawned via Node child_process)
  3. Config file at ~/.cursor/logfire-config.json with shape:
       {"token": "pylf_v1_...", "base_url": "https://..."}

Optional: LOGFIRE_LOCAL_LOG=true for local JSONL mirror.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

VERSION = "0.1.0"
SERVICE_NAME = "cursor-logfire-plugin"
AGENT_NAME = "cursor"

CONFIG_FILE = Path.home() / ".cursor" / "logfire-config.json"


def _read_config_file() -> dict:
    """Load ~/.cursor/logfire-config.json if present, otherwise empty dict."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}


def _read_windows_user_env(name: str) -> str:
    """Read a User-scope env var directly from HKCU\\Environment.

    Required because Cursor on Windows spawns hook subprocesses without
    inheriting the User-scope environment (only the parent Cursor process's
    env at startup, which itself may be stale).
    """
    if sys.platform != "win32":
        return ""
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
            return value if isinstance(value, str) else ""
    except (FileNotFoundError, OSError, ImportError):
        return ""


def get_logfire_setting(name: str, default: str = "") -> str:
    """Resolve a Logfire setting from env, Windows registry, then config file.

    The config-file key is the lowercased name with the LOGFIRE_ prefix
    stripped (e.g. LOGFIRE_TOKEN -> "token", LOGFIRE_BASE_URL -> "base_url").
    """
    value = os.environ.get(name, "")
    if value:
        return value
    value = _read_windows_user_env(name)
    if value:
        return value
    config_key = name.lower()
    if config_key.startswith("logfire_"):
        config_key = config_key[len("logfire_") :]
    return _read_config_file().get(config_key, default)


OTLP_EVENTS = {
    "SessionStart",
    "sessionStart",
    "Stop",
    "stop",
    "SubagentStop",
    "subagentStop",
    "SessionEnd",
    "sessionEnd",
}

MODEL_PRICING: dict[str, tuple[float, float]] = {
    "opus": (0.000015, 0.000075),
    "sonnet": (0.000003, 0.000015),
    "haiku": (0.0000008, 0.000004),
    "gpt-4o": (0.0000025, 0.00001),
    "gpt-4.1": (0.000002, 0.000008),
    "gpt-4.1-mini": (0.0000004, 0.0000016),
    "gpt-4.1-nano": (0.0000001, 0.0000004),
    "o3": (0.00001, 0.00004),
    "o4-mini": (0.0000011, 0.0000044),
    "gemini": (0.0000025, 0.00001),
}

TOOL_CATEGORIES: dict[str, str] = {
    "Read": "file_ops",
    "Write": "file_ops",
    "Edit": "file_ops",
    "MultiEdit": "file_ops",
    "Glob": "search",
    "Grep": "search",
    "LS": "search",
    "Bash": "execution",
    "Shell": "execution",
    "WebSearch": "web",
    "WebFetch": "web",
    "Task": "agent",
    "Delete": "file_ops",
}


def categorize_tool(name: str) -> str:
    if name in TOOL_CATEGORIES:
        return TOOL_CATEGORIES[name]
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def format_tool_name(name: str) -> str:
    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        if len(parts) == 3:
            return f"{parts[1]}/{parts[2]}"
    return name


_hook_event = "unknown"
_session_id = "unknown"
_diag_log: str | None = None


def log_diag(level: str, msg: str, detail: str | None = None) -> None:
    if not _diag_log:
        return
    entry: dict = {
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "level": level,
        "hook_event": _hook_event,
        "session_id": _session_id,
        "message": msg,
    }
    if detail:
        entry["detail"] = detail
    try:
        with open(_diag_log, "a") as f:
            f.write(json.dumps(entry) + "\n")
    except OSError:
        pass


def now_nano() -> int:
    return time.time_ns()


def random_span_id() -> str:
    return os.urandom(8).hex()


def trace_id_from_session(session_id: str) -> str:
    return hashlib.sha256(session_id.encode()).hexdigest()[:32]


def iso_to_nano(iso_str: str) -> int | None:
    try:
        ts = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(ts.timestamp() * 1e9)
    except (ValueError, AttributeError):
        return None


def to_otlp_anyvalue(val: object) -> dict:
    if isinstance(val, bool):
        return {"boolValue": val}
    if isinstance(val, int):
        return {"intValue": str(val)}
    if isinstance(val, float):
        return {"doubleValue": val}
    if val is None:
        return {"stringValue": ""}
    if isinstance(val, str):
        return {"stringValue": val}
    if isinstance(val, list):
        return {"arrayValue": {"values": [to_otlp_anyvalue(v) for v in val]}}
    if isinstance(val, dict):
        return {
            "kvlistValue": {
                "values": [
                    {"key": k, "value": to_otlp_anyvalue(v)} for k, v in val.items()
                ]
            }
        }
    return {"stringValue": str(val)}


def make_attr(key: str, val: str) -> dict:
    return {"key": key, "value": {"stringValue": val}}


def make_int_attr(key: str, val: int) -> dict:
    return {"key": key, "value": {"intValue": str(val)}}


def make_double_attr(key: str, val: float) -> dict:
    return {"key": key, "value": {"doubleValue": val}}


def make_complex_attr(key: str, val: object) -> dict:
    return {"key": key, "value": to_otlp_anyvalue(val)}


def build_span(trace_id, span_id, parent_span_id, name, start_ns, end_ns, attrs):
    span = {
        "traceId": trace_id,
        "spanId": span_id,
        "name": name,
        "kind": 1,
        "startTimeUnixNano": str(start_ns),
        "endTimeUnixNano": str(end_ns),
        "attributes": attrs,
        "status": {"code": 1},
    }
    if parent_span_id:
        span["parentSpanId"] = parent_span_id
    return span


def build_otlp_envelope(spans):
    return {
        "resourceSpans": [
            {
                "resource": {
                    "attributes": [
                        make_attr("service.name", SERVICE_NAME),
                        make_attr("service.version", VERSION),
                    ]
                },
                "scopeSpans": [
                    {
                        "scope": {"name": "cursor-logfire", "version": VERSION},
                        "spans": spans,
                    }
                ],
            }
        ]
    }


def send_otlp(payload, endpoint, token):
    data = json.dumps(payload).encode()
    req = Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
            "User-Agent": SERVICE_NAME,
        },
        method="POST",
    )
    try:
        with urlopen(req, timeout=5):
            pass
    except HTTPError as e:
        log_diag("warn", "OTLP export failed", f"http_status={e.code}")
    except (URLError, OSError):
        log_diag("warn", "OTLP export failed (network/timeout)")


def get_model_prices(model):
    for key, prices in MODEL_PRICING.items():
        if key in model:
            return prices
    return None


def calculate_cost(model, raw_input, output_tokens, cache_creation=0, cache_read=0):
    prices = get_model_prices(model)
    if not prices:
        return None
    ip, op = prices
    return (
        (raw_input * ip)
        + (cache_creation * ip * 1.25)
        + (cache_read * ip * 0.1)
        + (output_tokens * op)
    )


def read_state(state_file):
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state_file, state):
    fd, tmp = tempfile.mkstemp(
        prefix=os.path.basename(state_file) + ".", dir=os.path.dirname(state_file)
    )
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(state, f)
        os.replace(tmp, state_file)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def acquire_lock(lock_dir):
    for _ in range(50):
        try:
            os.mkdir(lock_dir)
            return True
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(lock_dir) > 30:
                    try:
                        os.rmdir(lock_dir)
                    except OSError:
                        pass
                    continue
            except OSError:
                pass
            time.sleep(0.1)
    return False


def release_lock(lock_dir):
    try:
        os.rmdir(lock_dir)
    except OSError:
        pass


def _infer_finish_reason(assistant_msg):
    stop_reason = assistant_msg.get("message", {}).get("stop_reason")
    if stop_reason == "end_turn":
        return "stop"
    if stop_reason == "tool_use":
        return "tool_call"
    if stop_reason is not None:
        return stop_reason
    content = assistant_msg.get("message", {}).get("content", [])
    if any(c.get("type") == "tool_use" for c in content if isinstance(c, dict)):
        return "tool_call"
    return "stop"


USER_QUERY_RE = re.compile(r"<user_query>(.*?)</user_query>", re.DOTALL)
CURSOR_CONTEXT_TAGS = (
    "image_files",
    "attached_files",
    "external_links",
    "system_reminder",
    "system_notification",
    "previous_tool_call",
    "open_and_recently_viewed_files",
    "git_status",
)


def _clean_user_text(raw: str) -> str:
    """Strip Cursor's prompt-wrapper tags from a user-message text block.

    Cursor wraps real user input in ``<user_query>...</user_query>`` and
    prepends other tags (``<image_files>``, ``<attached_files>``,
    ``<external_links>``, ``<system_reminder>``, ``<system_notification>``,
    ``<open_and_recently_viewed_files>``, ``<git_status>``, ...) for context
    the LLM sees but the user did not type. Extract the user_query payload
    as the visible content and append a compact annotation listing any
    Cursor-injected context that was also present.
    """
    if not raw:
        return ""
    matches = USER_QUERY_RE.findall(raw)
    extras = [tag for tag in CURSOR_CONTEXT_TAGS if f"<{tag}>" in raw]
    cleaned = "\n\n".join(m.strip() for m in matches if m.strip())
    if cleaned and extras:
        return f"{cleaned}\n\n[Cursor context: {', '.join(extras)}]"
    if cleaned:
        return cleaned
    if extras:
        return f"[Cursor context only: {', '.join(extras)}]"
    return raw.strip()


def _convert_input_message(line):
    content = line.get("message", {}).get("content", "")
    parts = []
    if isinstance(content, str):
        parts.append({"type": "text", "content": _clean_user_text(content)})
    elif isinstance(content, list):
        for block in content:
            if not isinstance(block, dict):
                parts.append({"type": "text", "content": str(block)})
                continue
            if block.get("type") == "tool_result":
                result_content = block.get("content", "")
                if isinstance(result_content, str):
                    try:
                        result_content = json.loads(result_content)
                    except (json.JSONDecodeError, TypeError):
                        pass
                parts.append(
                    {
                        "type": "tool_call_response",
                        "id": block.get("tool_use_id"),
                        "name": block.get("name"),
                        "result": result_content,
                    }
                )
            elif block.get("type") == "text":
                raw_text = block.get("text") or block.get("content", "")
                parts.append({"type": "text", "content": _clean_user_text(raw_text)})
            else:
                parts.append({"type": "text", "content": json.dumps(block)})
    else:
        parts.append({"type": "text", "content": _clean_user_text(str(content))})
    return {"role": "user", "parts": parts}


def _convert_output_message(line):
    content = line.get("message", {}).get("content", [])
    finish_reason = _infer_finish_reason(line)
    parts = []
    for block in content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "text":
            parts.append({"type": "text", "content": block.get("text", "")})
        elif btype == "thinking":
            parts.append({"type": "thinking", "thinking": block.get("thinking", "")})
        elif btype == "tool_use":
            parts.append(
                {
                    "type": "tool_call",
                    "id": block.get("id"),
                    "name": block.get("name"),
                    "arguments": json.dumps(block.get("input", {})),
                }
            )
    return {"role": "assistant", "parts": parts, "finish_reason": finish_reason}


def parse_transcript_slice(transcript_path, last_line):
    """Parse new transcript lines from a Cursor agent-transcripts JSONL file.

    Cursor's transcript format differs from Claude Code's:
      - Top-level key is ``role`` (not ``type``)
      - Each line is one complete LLM API call (no streaming fragments)
      - User messages contain only ``text`` blocks (Cursor does not echo
        tool_result blocks back into the transcript)
      - Assistant messages contain ``text``, ``thinking``, and ``tool_use``
        blocks batched into a single line
      - No ``message.id``, ``message.model``, ``message.usage``,
        ``message.stop_reason``, ``timestamp``, or ``uuid`` per line

    One Cursor ``stop`` event represents one full user turn, which typically
    contains 1 user line followed by N assistant lines (one per tool-use
    cycle). The token counts in the stop payload are totals for the whole
    turn, so we collapse the slice into ONE chat span per stop with all
    input messages and all output messages attached.

    Returns ``(input_messages, output_messages, all_new_messages,
    new_total_lines)``. The first three are pydantic-ai-shaped dicts.
    ``all_new_messages`` is the ordered combined list (user + assistant)
    suitable for appending to ``state['all_messages']`` for the root span.
    """
    if not transcript_path or not os.path.isfile(transcript_path):
        return [], [], [], last_line

    try:
        with open(transcript_path, encoding="utf-8") as f:
            all_lines_raw = f.readlines()
    except OSError:
        return [], [], [], last_line

    total_lines = len(all_lines_raw)
    if total_lines <= last_line:
        return [], [], [], last_line

    parsed = []
    for raw in all_lines_raw[last_line:]:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            continue

    input_messages: list[dict] = []
    output_messages: list[dict] = []
    all_new_messages: list[dict] = []
    for line in parsed:
        role = line.get("role")
        if role == "user":
            msg = _convert_input_message(line)
            input_messages.append(msg)
            all_new_messages.append(msg)
        elif role == "assistant":
            msg = _convert_output_message(line)
            output_messages.append(msg)
            all_new_messages.append(msg)

    return input_messages, output_messages, all_new_messages, total_lines


def _extract_user_snippet(messages: list[dict], max_len: int = 80) -> str | None:
    """Pull a short label from the most recent user text for logfire.msg.

    Skips messages that are only Cursor-context annotations (e.g. an image
    upload with no typed text), so the span title reflects what the user
    actually wrote.
    """
    for msg in reversed(messages):
        if msg.get("role") != "user":
            continue
        for part in msg.get("parts", []):
            if part.get("type") != "text":
                continue
            text = (part.get("content") or "").strip()
            if not text or text.startswith("[Cursor context only:"):
                continue
            first_line = text.split("\n", 1)[0].strip()
            if not first_line:
                continue
            if len(first_line) > max_len:
                return first_line[:max_len] + "..."
            return first_line
    return None


def _extract_tool_names(messages: list[dict]) -> list[str]:
    names: list[str] = []
    for msg in messages:
        for part in msg.get("parts", []):
            if part.get("type") == "tool_call":
                name = part.get("name", "")
                if name and name not in names:
                    names.append(name)
    return names


def handle_session_start(
    inp,
    state_file,
    lock_file,
    ts_nano,
    transcript_path,
    trace_id,
    otlp_endpoint,
    logfire_token,
    session_id,
):
    if not acquire_lock(lock_file):
        return
    try:
        cwd = inp.get("cwd", "") or (
            inp.get("workspace_roots", [""])[0] if inp.get("workspace_roots") else ""
        )
        model = inp.get("model", "")
        try:
            os.unlink(state_file)
        except OSError:
            pass
        state = _init_default_state(ts_nano, transcript_path or "", cwd, model)
        root_span_id = state["root_span_id"]
        write_state(state_file, state)
        attrs = [
            make_attr("logfire.msg", "Cursor session"),
            make_attr("logfire.span_type", "pending_span"),
            make_attr("logfire.pending_parent_id", "0000000000000000"),
            make_attr("agent_name", AGENT_NAME),
            make_attr("gen_ai.agent.name", AGENT_NAME),
            make_attr("session.id", session_id),
        ]
        if model:
            attrs.append(make_attr("gen_ai.response.model", model))
        if cwd:
            attrs.append(make_attr("session.cwd", cwd))
        span = build_span(
            trace_id,
            random_span_id(),
            root_span_id,
            "Cursor session",
            ts_nano,
            ts_nano,
            attrs,
        )
        send_otlp(build_otlp_envelope([span]), otlp_endpoint, logfire_token)
    finally:
        release_lock(lock_file)


def _init_default_state(
    ts_nano: int,
    transcript_path: str = "",
    cwd: str = "",
    model: str = "",
    skip_existing_transcript: bool = True,
) -> dict:
    """Build a fresh state dict.

    Used by sessionStart and as a lazy-init fallback in stop/sessionEnd when
    the plugin was installed mid-conversation (no prior sessionStart state).

    ``skip_existing_transcript=True`` (sessionStart): seed ``last_line`` with
    the current line count so we ignore any pre-existing content.
    ``skip_existing_transcript=False`` (lazy-init mid-session): seed
    ``last_line=0`` so the whole in-progress conversation is captured on the
    first stop event after install.
    """
    initial_line = 0
    if skip_existing_transcript and transcript_path and os.path.isfile(transcript_path):
        try:
            with open(transcript_path, encoding="utf-8") as f:
                initial_line = sum(1 for _ in f)
        except OSError:
            pass
    return {
        "root_span_id": random_span_id(),
        "start_time": str(ts_nano),
        "cwd": cwd,
        "model": model,
        "transcript_path": transcript_path,
        "last_line": initial_line,
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        "cost_details": [],
        "all_messages": [],
        "tools_meta": {"tools_used": {}, "categories": [], "skills": []},
    }


def handle_stop(
    inp,
    state_file,
    lock_file,
    trace_id,
    ts_nano,
    transcript_path,
    otlp_endpoint,
    logfire_token,
    hook_event="stop",
):
    if not acquire_lock(lock_file):
        return
    try:
        state = read_state(state_file)
        if not state:
            cwd = inp.get("cwd", "") or (
                inp.get("workspace_roots", [""])[0]
                if inp.get("workspace_roots")
                else ""
            )
            state = _init_default_state(
                ts_nano,
                transcript_path or "",
                cwd,
                inp.get("model", ""),
                skip_existing_transcript=False,
            )
            log_diag(
                "info",
                "stop fired without prior sessionStart state - lazily "
                "initialising state mid-session (will capture full "
                "in-progress conversation)",
                f"root_span_id={state['root_span_id']}",
            )
            write_state(state_file, state)
        root_span_id = state["root_span_id"]

        call_model = inp.get("model") or state.get("model", "")
        raw_input_tokens = inp.get("input_tokens", 0) or 0
        output_tokens = inp.get("output_tokens", 0) or 0
        cache_read = inp.get("cache_read_tokens", 0) or 0
        cache_write = inp.get("cache_write_tokens", 0) or 0
        has_tokens = bool(
            raw_input_tokens or output_tokens or cache_read or cache_write
        )

        # Pull conversation content for this turn from the transcript slice.
        # Each Cursor stop covers one user turn (1 user line + N assistant
        # lines); we attach all of them so Logfire shows the full exchange.
        effective_tp = transcript_path or state.get("transcript_path", "") or ""
        last_line = state.get("last_line", 0)
        input_msgs, output_msgs, new_msgs, new_total = parse_transcript_slice(
            effective_tp, last_line
        )

        if not has_tokens and not new_msgs:
            log_diag(
                "info",
                "stop with no token usage and no new transcript content, skipping span",
                f"model={call_model}",
            )
            return

        input_tokens_total = raw_input_tokens + cache_read + cache_write
        cost = calculate_cost(
            call_model, raw_input_tokens, output_tokens, cache_write, cache_read
        )
        generation_id = inp.get("generation_id", "")
        loop_count = inp.get("loop_count", 0) or 0
        status = inp.get("status", "")

        user_snippet = _extract_user_snippet(input_msgs)
        tool_names = _extract_tool_names(output_msgs)
        if user_snippet and tool_names:
            tool_summary = ", ".join(tool_names[:3])
            if len(tool_names) > 3:
                tool_summary += f" +{len(tool_names) - 3} more"
            logfire_msg = f"User: {user_snippet} -> {tool_summary}"
        elif user_snippet:
            logfire_msg = f"User: {user_snippet} -> Response"
        elif tool_names:
            logfire_msg = f"Response: {', '.join(tool_names[:3])}"
        else:
            logfire_msg = f"chat {call_model}"

        finish_reason = "tool_call" if tool_names else "stop"

        attrs = [
            make_attr("logfire.msg", logfire_msg),
            make_attr("logfire.span_type", "span"),
            make_attr("gen_ai.operation.name", "chat"),
            make_attr("gen_ai.system", "anthropic"),
            make_attr("gen_ai.request.model", call_model),
            make_attr("gen_ai.response.model", call_model),
            make_int_attr("gen_ai.usage.input_tokens", input_tokens_total),
            make_int_attr("gen_ai.usage.output_tokens", output_tokens),
            make_int_attr("gen_ai.usage.cache_read_input_tokens", cache_read),
            make_int_attr("gen_ai.usage.cache_creation_input_tokens", cache_write),
            make_complex_attr("gen_ai.input.messages", input_msgs),
            make_complex_attr("gen_ai.output.messages", output_msgs),
            make_complex_attr("gen_ai.response.finish_reasons", [finish_reason]),
        ]
        if cost is not None:
            attrs.append(make_double_attr("operation.cost", cost))
        if generation_id:
            attrs.append(make_attr("generation.id", generation_id))
        if status:
            attrs.append(make_attr("generation.status", status))
        if loop_count:
            attrs.append(make_int_attr("generation.loop_count", loop_count))
        if hook_event:
            attrs.append(make_attr("hook.event", hook_event))
        if tool_names:
            attrs.append(make_complex_attr("cursor.tools_used", tool_names))

        json_schema: dict = {
            "type": "object",
            "properties": {
                "gen_ai.input.messages": {"type": "array"},
                "gen_ai.output.messages": {"type": "array"},
                "gen_ai.response.finish_reasons": {"type": "array"},
            },
        }
        if tool_names:
            json_schema["properties"]["cursor.tools_used"] = {"type": "array"}
        attrs.append(make_complex_attr("logfire.json_schema", json_schema))

        span = build_span(
            trace_id,
            random_span_id(),
            root_span_id,
            f"chat {call_model}",
            ts_nano,
            ts_nano,
            attrs,
        )
        send_otlp(build_otlp_envelope([span]), otlp_endpoint, logfire_token)

        # Re-read state in case of concurrent modification, then update.
        state = read_state(state_file) or state
        usage = state.setdefault(
            "usage",
            {
                "input_tokens": 0,
                "output_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            },
        )
        usage["input_tokens"] = usage.get("input_tokens", 0) + raw_input_tokens
        usage["output_tokens"] = usage.get("output_tokens", 0) + output_tokens
        usage["cache_read_input_tokens"] = (
            usage.get("cache_read_input_tokens", 0) + cache_read
        )
        usage["cache_creation_input_tokens"] = (
            usage.get("cache_creation_input_tokens", 0) + cache_write
        )
        if call_model:
            state["model"] = call_model
        if effective_tp:
            state["transcript_path"] = effective_tp
        state["last_line"] = new_total
        if new_msgs:
            state["all_messages"] = state.get("all_messages", []) + new_msgs
        write_state(state_file, state)
    finally:
        release_lock(lock_file)


def handle_session_end(
    inp,
    state_file,
    lock_file,
    trace_id,
    ts_nano,
    transcript_path,
    otlp_endpoint,
    logfire_token,
    session_id,
):
    state = read_state(state_file)
    if not state:
        cwd = inp.get("cwd", "") or (
            inp.get("workspace_roots", [""])[0] if inp.get("workspace_roots") else ""
        )
        state = _init_default_state(
            ts_nano,
            transcript_path or "",
            cwd,
            inp.get("model", ""),
            skip_existing_transcript=False,
        )
        log_diag(
            "info",
            "sessionEnd fired without prior state - lazily initialising "
            "(will capture full conversation from transcript)",
            f"root_span_id={state['root_span_id']}",
        )
    if not acquire_lock(lock_file):
        return
    try:
        root_span_id = state["root_span_id"]
        start_time = state.get("start_time", str(ts_nano))
        cwd = state.get("cwd", "")
        model = state.get("model", "")

        # Final transcript parse to catch any remaining messages.
        effective_tp = transcript_path or state.get("transcript_path", "") or ""
        last_line = state.get("last_line", 0)
        if effective_tp and os.path.isfile(effective_tp):
            _, _, remaining_msgs, new_total = parse_transcript_slice(
                effective_tp, last_line
            )
            if remaining_msgs:
                state["all_messages"] = state.get("all_messages", []) + remaining_msgs
                state["last_line"] = new_total

        all_messages = state.get("all_messages", [])
        usage = state.get("usage", {})
        total_input = usage.get("input_tokens", 0)
        total_output = usage.get("output_tokens", 0)
        total_cache_read = usage.get("cache_read_input_tokens", 0)
        total_cache_write = usage.get("cache_creation_input_tokens", 0)
        duration_ms = inp.get("duration_ms", 0)
        reason = inp.get("reason", "unknown")
        final_status = inp.get("final_status", "")

        # Aggregate tool usage across the whole session for the root span.
        agg_tools = _extract_tool_names(
            [m for m in all_messages if m.get("role") == "assistant"]
        )

        # Pull a final result snippet (last assistant text) for at-a-glance UI.
        final_result: str | None = None
        for msg in reversed(all_messages):
            if msg.get("role") != "assistant":
                continue
            for part in reversed(msg.get("parts", [])):
                if part.get("type") == "text":
                    final_result = part.get("content")
                    break
            if final_result is not None:
                break

        attrs = [
            make_attr("logfire.msg", "Cursor session"),
            make_attr("logfire.span_type", "span"),
            make_attr("agent_name", AGENT_NAME),
            make_attr("gen_ai.agent.name", AGENT_NAME),
            make_attr("gen_ai.system", "anthropic"),
            make_attr("session.id", session_id),
            make_attr("session.end_reason", reason),
            make_int_attr(
                "gen_ai.usage.input_tokens",
                total_input + total_cache_read + total_cache_write,
            ),
            make_int_attr("gen_ai.usage.output_tokens", total_output),
            make_int_attr("gen_ai.usage.cache_read_input_tokens", total_cache_read),
            make_int_attr(
                "gen_ai.usage.cache_creation_input_tokens", total_cache_write
            ),
            make_complex_attr("pydantic_ai.all_messages", all_messages),
        ]
        if final_status:
            attrs.append(make_attr("session.final_status", final_status))
        if model:
            attrs.append(make_attr("gen_ai.response.model", model))
        if cwd:
            attrs.append(make_attr("session.cwd", cwd))
        if duration_ms:
            attrs.append(make_int_attr("session.duration_ms", duration_ms))
        if agg_tools:
            attrs.append(make_complex_attr("cursor.tools_used", agg_tools))
        if final_result is not None:
            attrs.append(make_complex_attr("final_result", final_result))
        total_cost = calculate_cost(
            model, total_input, total_output, total_cache_write, total_cache_read
        )
        if total_cost is not None:
            attrs.append(make_double_attr("operation.cost", total_cost))

        json_schema: dict = {
            "type": "object",
            "properties": {
                "pydantic_ai.all_messages": {"type": "array"},
            },
        }
        if agg_tools:
            json_schema["properties"]["cursor.tools_used"] = {"type": "array"}
        if final_result is not None:
            json_schema["properties"]["final_result"] = {"type": "string"}
        attrs.append(make_complex_attr("logfire.json_schema", json_schema))

        span = build_span(
            trace_id,
            root_span_id,
            "",
            "Cursor session",
            int(start_time),
            ts_nano,
            attrs,
        )
        send_otlp(build_otlp_envelope([span]), otlp_endpoint, logfire_token)
        try:
            os.unlink(state_file)
        except OSError:
            pass
    finally:
        release_lock(lock_file)


def main():
    global _hook_event, _session_id, _diag_log

    # Always log diagnostics until we've verified the transcript format
    log_dir = Path.home() / ".cursor" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    _diag_log = str(log_dir / "logfire-diagnostics.jsonl")

    # Read stdin as bytes and decode with utf-8-sig so a leading BOM
    # (which Cursor on Windows prepends) is stripped reliably regardless
    # of the platform-default text encoding for sys.stdin.
    try:
        raw_input = sys.stdin.buffer.read().decode("utf-8-sig")
    except (AttributeError, UnicodeDecodeError, OSError):
        raw_input = sys.stdin.read()
        if raw_input.startswith("\ufeff"):
            raw_input = raw_input[1:]
        elif raw_input.startswith("\xef\xbb\xbf"):
            raw_input = raw_input[3:]
    try:
        inp = json.loads(raw_input)
    except json.JSONDecodeError:
        log_diag("error", "Failed to parse hook input", raw_input[:500])
        return

    _hook_event = inp.get("hook_event_name", "unknown")
    _session_id = inp.get("session_id", inp.get("conversation_id", "unknown"))

    # Log the full hook input for format discovery
    log_diag("info", "Hook input received", json.dumps(inp, default=str)[:2000])

    # Dump transcript file contents for format discovery
    transcript_path = inp.get("transcript_path", "")
    if transcript_path and os.path.isfile(transcript_path):
        try:
            with open(transcript_path) as f:
                sample = f.read(2000)
            log_diag("info", "Transcript sample", sample)
        except OSError as e:
            log_diag("warn", "Could not read transcript", str(e))
    else:
        log_diag("info", "No transcript file", f"path={transcript_path!r}")

    if _hook_event not in OTLP_EVENTS:
        log_diag("info", "Skipping non-OTLP event", _hook_event)
        return

    logfire_token = get_logfire_setting("LOGFIRE_TOKEN")
    if not logfire_token:
        log_diag(
            "warn",
            "No LOGFIRE_TOKEN found in env, Windows registry, or "
            f"{CONFIG_FILE}; skipping export",
        )
        return

    base_url = get_logfire_setting(
        "LOGFIRE_BASE_URL", "https://logfire-us.pydantic.dev"
    ).rstrip("/")
    otlp_endpoint = f"{base_url}/v1/traces"

    session_id = inp.get("session_id", inp.get("conversation_id", ""))
    if not session_id:
        log_diag("warn", "No session_id or conversation_id")
        return

    trace_id = trace_id_from_session(session_id)
    ts_nano = now_nano()

    tmpdir = os.environ.get("TMPDIR") or os.environ.get("TEMP") or tempfile.gettempdir()
    state_file = os.path.join(tmpdir, f"cursor-logfire-{session_id}.json")
    lock_file = f"{state_file}.lock"

    normalized = _hook_event.lower()
    if normalized == "sessionstart":
        handle_session_start(
            inp,
            state_file,
            lock_file,
            ts_nano,
            transcript_path,
            trace_id,
            otlp_endpoint,
            logfire_token,
            session_id,
        )
    elif normalized in ("stop", "subagentstop"):
        handle_stop(
            inp,
            state_file,
            lock_file,
            trace_id,
            ts_nano,
            transcript_path,
            otlp_endpoint,
            logfire_token,
            _hook_event,
        )
    elif normalized == "sessionend":
        handle_session_end(
            inp,
            state_file,
            lock_file,
            trace_id,
            ts_nano,
            transcript_path,
            otlp_endpoint,
            logfire_token,
            session_id,
        )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            log_diag("error", "Unexpected failure", str(sys.exc_info()[1]))
        except Exception:
            pass
    sys.exit(0)
