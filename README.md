# HarnessTalk-Bridge

Local MCP server for inter-agent consultation across Hermes, OpenClaw,
and the Claude API.

## Requirements

- Python 3.11+
- A configured target file at `config/targets.toml`

## Setup

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
```

## Run

Default stdio transport:

```bash
agent-bridge
```

Streamable HTTP on loopback:

```bash
agent-bridge --transport streamable-http
```

## Test

```bash
pytest
```
