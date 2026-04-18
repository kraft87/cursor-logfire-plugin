#!/usr/bin/env python3
"""Logfire trace exporter for Cursor IDE.

Ported from pydantic/claude-code-logfire-plugin.
Captures session traces via Cursor's hooks system and sends to Logfire as OTel spans.

Trace hierarchy:
  Cursor session (root span)
  +-- chat model-name       <- LLM API call 1
  +-- chat model-name       <- LLM API call 2
  ...

Set LOGFIRE_TOKEN env var to enable. Optionally set LOGFIRE_LOCAL_LOG=true for local JSONL.
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

OTLP_EVENTS = {"SessionStart", "sessionStart", "Stop", "stop", "SubagentStop", "subagentStop", "SessionEnd", "sessionEnd"}

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
        return {"kvlistValue": {"values": [{"key": k, "value": to_otlp_anyvalue(v)} for k, v in val.items()]}}
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
        "resourceSpans": [{
            "resource": {
                "attributes": [
                    make_attr("service.name", SERVICE_NAME),
                    make_attr("service.version", VERSION),
                ]
            },
            "scopeSpans": [{
                "scope": {"name": "cursor-logfire", "version": VERSION},
                "spans": spans,
            }],
        }]
    }


def send_otlp(payload, endpoint, token):
    data = json.dumps(payload).encode()
    req = Request(
        endpoint, data=data,
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
    return (raw_input * ip) + (cache_creation * ip * 1.25) + (cache_read * ip * 0.1) + (output_tokens * op)


def read_state(state_file):
    try:
        with open(state_file) as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def write_state(state_file, state):
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(state_file) + ".", dir=os.path.dirname(state_file))
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


def _convert_input_message(line):
    content = line.get("message", {}).get("content", "")
    parts = []
    if isinstance(content, str):
        parts.append({"type": "text", "content": content})
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
                parts.append({
                    "type": "tool_call_response",
                    "id": block.get("tool_use_id"),
                    "name": block.get("name"),
                    "result": result_content,
                })
            elif block.get("type") == "text":
                parts.append({"type": "text", "content": block.get("text") or block.get("content", "")})
            else:
                parts.append({"type": "text", "content": json.dumps(block)})
    else:
        parts.append({"type": "text", "content": str(content)})
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
            parts.append({
                "type": "tool_call",
                "id": block.get("id"),
                "name": block.get("name"),
                "arguments": json.dumps(block.get("input", {})),
            })
    return {"role": "assistant", "parts": parts, "finish_reason": finish_reason}


def _merge_assistant_content(base, new):
    base_msg = base.setdefault("message", {})
    base_content = base_msg.setdefault("content", [])
    new_content = new.get("message", {}).get("content", [])
    seen_tool_ids = {b["id"] for b in base_content if isinstance(b, dict) and b.get("type") == "tool_use" and "id" in b}
    for block in new_content:
        if not isinstance(block, dict):
            continue
        btype = block.get("type")
        if btype == "tool_use":
            if block.get("id") not in seen_tool_ids:
                base_content.append(block)
                seen_tool_ids.add(block["id"])
        elif btype == "thinking":
            for i, b in enumerate(base_content):
                if isinstance(b, dict) and b.get("type") == "thinking":
                    base_content[i] = block
                    break
            else:
                base_content.append(block)
        elif btype == "text":
            for i in range(len(base_content) - 1, -1, -1):
                if isinstance(base_content[i], dict) and base_content[i].get("type") == "text":
                    base_content[i] = block
                    break
            else:
                base_content.append(block)
        else:
            base_content.append(block)
    new_msg = new.get("message", {})
    if new_msg.get("stop_reason") is not None:
        base_msg["stop_reason"] = new_msg["stop_reason"]
    if new_msg.get("usage"):
        base_msg["usage"] = new_msg["usage"]
    if new.get("timestamp"):
        base["timestamp"] = new["timestamp"]


def parse_transcript_slice(transcript_path, last_line):
    if not transcript_path or not os.path.isfile(transcript_path):
        return [], last_line
    try:
        with open(transcript_path) as f:
            all_lines_raw = f.readlines()
    except OSError:
        return [], last_line
    total_lines = len(all_lines_raw)
    if total_lines <= last_line:
        return [], last_line
    new_lines_raw = all_lines_raw[last_line:]
    parsed = []
    for raw in new_lines_raw:
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(json.loads(raw))
        except json.JSONDecodeError:
            continue
    relevant = [line for line in parsed if line.get("type") in ("user", "assistant")]
    seen = {}
    order = []
    for line in relevant:
        if line.get("type") == "assistant":
            key = line.get("message", {}).get("id") or line.get("uuid", id(line))
        else:
            key = line.get("uuid", id(line))
        key = str(key)
        if key not in seen:
            order.append(key)
            seen[key] = line
        elif line.get("type") == "assistant":
            _merge_assistant_content(seen[key], line)
        else:
            seen[key] = line
    deduped = [seen[k] for k in order]
    assistants = [line for line in deduped if line.get("type") == "assistant"]
    if not assistants:
        return [], total_lines
    api_calls = []
    current_users = []
    prev_assistant_msg = None
    for entry in deduped:
        if entry.get("type") == "user":
            current_users.append(entry)
        elif entry.get("type") == "assistant":
            asst = entry
            call_model = asst.get("message", {}).get("model", "")
            usage = asst.get("message", {}).get("usage", {})
            input_msgs = []
            if prev_assistant_msg is not None:
                input_msgs.append(_convert_output_message(prev_assistant_msg))
            input_msgs.extend(_convert_input_message(u) for u in current_users)
            api_calls.append({
                "model": call_model,
                "timestamp": asst.get("timestamp", ""),
                "stop_reason": _infer_finish_reason(asst),
                "usage": usage,
                "input_messages": input_msgs,
                "output_messages": [_convert_output_message(asst)],
            })
            prev_assistant_msg = asst
            current_users = []
    return api_calls, total_lines


def handle_session_start(inp, state_file, lock_file, ts_nano, transcript_path, trace_id, otlp_endpoint, logfire_token, session_id):
    if not acquire_lock(lock_file):
        return
    try:
        root_span_id = random_span_id()
        cwd = inp.get("cwd", "") or (inp.get("workspace_roots", [""])[0] if inp.get("workspace_roots") else "")
        model = inp.get("model", "")
        initial_line = 0
        if transcript_path and os.path.isfile(transcript_path):
            try:
                with open(transcript_path) as f:
                    initial_line = sum(1 for _ in f)
            except OSError:
                pass
        try:
            os.unlink(state_file)
        except OSError:
            pass
        state = {
            "root_span_id": root_span_id,
            "start_time": str(ts_nano),
            "cwd": cwd,
            "model": model,
            "transcript_path": transcript_path,
            "last_line": initial_line,
            "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
            "cost_details": [],
            "all_messages": [],
            "tools_meta": {"tools_used": {}, "categories": [], "skills": []},
        }
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
        span = build_span(trace_id, random_span_id(), root_span_id, "Cursor session", ts_nano, ts_nano, attrs)
        send_otlp(build_otlp_envelope([span]), otlp_endpoint, logfire_token)
    finally:
        release_lock(lock_file)


def handle_stop(inp, state_file, lock_file, trace_id, ts_nano, transcript_path, otlp_endpoint, logfire_token, hook_event="stop"):
    if not acquire_lock(lock_file):
        return
    try:
        state = read_state(state_file)
        if not state:
            return
        root_span_id = state["root_span_id"]
        last_line = state.get("last_line", 0)
        model_default = state.get("model", "")
        api_calls, new_total = parse_transcript_slice(transcript_path, last_line)
        if not api_calls:
            return
        spans = []
        new_messages = []
        for call in api_calls:
            call_model = call.get("model") or model_default
            usage = call.get("usage", {})
            raw_input = usage.get("input_tokens", 0) or 0
            output_tokens = usage.get("output_tokens", 0) or 0
            cache_creation = usage.get("cache_creation_input_tokens", 0) or 0
            cache_read = usage.get("cache_read_input_tokens", 0) or 0
            input_tokens = raw_input + cache_creation + cache_read
            cost = calculate_cost(call_model, raw_input, output_tokens, cache_creation, cache_read)
            call_ns = ts_nano
            if call.get("timestamp"):
                parsed_ns = iso_to_nano(call["timestamp"])
                if parsed_ns:
                    call_ns = parsed_ns
            span_id = random_span_id()
            attrs = [
                make_attr("logfire.msg", f"chat {call_model}"),
                make_attr("logfire.span_type", "span"),
                make_attr("gen_ai.operation.name", "chat"),
                make_attr("gen_ai.request.model", call_model),
                make_attr("gen_ai.response.model", call_model),
                make_int_attr("gen_ai.usage.input_tokens", input_tokens),
                make_int_attr("gen_ai.usage.output_tokens", output_tokens),
            ]
            if cost is not None:
                attrs.append(make_double_attr("operation.cost", cost))
            spans.append(build_span(trace_id, span_id, root_span_id, f"chat {call_model}", call_ns, call_ns, attrs))
            state["usage"]["input_tokens"] = state["usage"].get("input_tokens", 0) + input_tokens
            state["usage"]["output_tokens"] = state["usage"].get("output_tokens", 0) + output_tokens
        if spans:
            send_otlp(build_otlp_envelope(spans), otlp_endpoint, logfire_token)
        state["last_line"] = new_total
        write_state(state_file, state)
    finally:
        release_lock(lock_file)


def handle_session_end(inp, state_file, lock_file, trace_id, ts_nano, transcript_path, otlp_endpoint, logfire_token, session_id):
    state = read_state(state_file)
    if not state:
        return
    if not acquire_lock(lock_file):
        return
    try:
        root_span_id = state["root_span_id"]
        start_time = state.get("start_time", str(ts_nano))
        cwd = state.get("cwd", "")
        model = state.get("model", "")
        usage = state.get("usage", {})
        total_input = usage.get("input_tokens", 0)
        total_output = usage.get("output_tokens", 0)
        duration_ms = inp.get("duration_ms", 0)
        reason = inp.get("reason", "unknown")
        attrs = [
            make_attr("logfire.msg", "Cursor session"),
            make_attr("logfire.span_type", "span"),
            make_attr("agent_name", AGENT_NAME),
            make_attr("gen_ai.agent.name", AGENT_NAME),
            make_attr("session.id", session_id),
            make_attr("session.end_reason", reason),
            make_int_attr("gen_ai.usage.input_tokens", total_input),
            make_int_attr("gen_ai.usage.output_tokens", total_output),
        ]
        if model:
            attrs.append(make_attr("gen_ai.response.model", model))
        if cwd:
            attrs.append(make_attr("session.cwd", cwd))
        if duration_ms:
            attrs.append(make_int_attr("session.duration_ms", duration_ms))
        total_cost = calculate_cost(model, total_input, total_output)
        if total_cost is not None:
            attrs.append(make_double_attr("operation.cost", total_cost))
        span = build_span(trace_id, root_span_id, "", "Cursor session", int(start_time), ts_nano, attrs)
        send_otlp(build_otlp_envelope([span]), otlp_endpoint, logfire_token)
        try:
            os.unlink(state_file)
        except OSError:
            pass
    finally:
        release_lock(lock_file)


def main():
    global _hook_event, _session_id, _diag_log

    enable_local_log = os.environ.get("LOGFIRE_LOCAL_LOG", "false") in ("true", "1")
    enable_diagnostics = os.environ.get("LOGFIRE_DIAGNOSTICS", "false") in ("true", "1")

    if enable_diagnostics or enable_local_log:
        log_dir = Path.home() / ".cursor" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        _diag_log = str(log_dir / "logfire-diagnostics.jsonl")

    raw_input = sys.stdin.read()
    try:
        inp = json.loads(raw_input)
    except json.JSONDecodeError:
        log_diag("error", "Failed to parse hook input", raw_input[:500])
        return

    _hook_event = inp.get("hook_event_name", "unknown")
    _session_id = inp.get("session_id", "unknown")

    if _hook_event not in OTLP_EVENTS:
        return

    logfire_token = os.environ.get("LOGFIRE_TOKEN", "")
    if not logfire_token:
        return

    base_url = os.environ.get("LOGFIRE_BASE_URL", "https://logfire-us.pydantic.dev").rstrip("/")
    otlp_endpoint = f"{base_url}/v1/traces"

    session_id = inp.get("session_id", "")
    if not session_id:
        return

    trace_id = trace_id_from_session(session_id)
    ts_nano = now_nano()
    transcript_path = inp.get("transcript_path", "")

    tmpdir = os.environ.get("TMPDIR") or os.environ.get("TEMP") or tempfile.gettempdir()
    state_file = os.path.join(tmpdir, f"cursor-logfire-{session_id}.json")
    lock_file = f"{state_file}.lock"

    normalized = _hook_event.lower()
    if normalized == "sessionstart":
        handle_session_start(inp, state_file, lock_file, ts_nano, transcript_path, trace_id, otlp_endpoint, logfire_token, session_id)
    elif normalized in ("stop", "subagentstop"):
        handle_stop(inp, state_file, lock_file, trace_id, ts_nano, transcript_path, otlp_endpoint, logfire_token, _hook_event)
    elif normalized == "sessionend":
        handle_session_end(inp, state_file, lock_file, trace_id, ts_nano, transcript_path, otlp_endpoint, logfire_token, session_id)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        try:
            log_diag("error", "Unexpected failure", str(sys.exc_info()[1]))
        except Exception:
            pass
    sys.exit(0)
