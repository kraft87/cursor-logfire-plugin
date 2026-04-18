# Logfire Session Capture for Cursor

Captures Cursor IDE session traces and sends them to [Pydantic Logfire](https://logfire.pydantic.dev) as OpenTelemetry spans.

## Install

In Cursor, run:
```
/plugin-add kraft87/cursor-logfire-plugin
```

## Setup

Set `LOGFIRE_TOKEN` in your environment:

**Windows (PowerShell):**
```powershell
[System.Environment]::SetEnvironmentVariable("LOGFIRE_TOKEN", "your-token", "User")
```

**macOS/Linux:**
```bash
echo 'export LOGFIRE_TOKEN="your-token"' >> ~/.zshrc
```

Then restart Cursor.

## What you get

Every Cursor session produces a trace in Logfire:
```
Cursor session              <- root span
├── chat claude-sonnet-4-6  <- LLM API call 1
├── chat gpt-4.1            <- LLM API call 2
└── chat claude-sonnet-4-6  <- LLM API call 3
```

Each span includes token usage, cost estimates, and model info.

## Debugging

Set `LOGFIRE_DIAGNOSTICS=true` to write diagnostics to `~/.cursor/logs/logfire-diagnostics.jsonl`.

## Requirements

- Python 3.7+ (stdlib only, no pip dependencies)
- A [Logfire](https://logfire.pydantic.dev) account with a write token
