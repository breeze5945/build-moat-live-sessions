# ChatGPT Task Scheduler — Challenge Track

## Setup

```bash
cd chatgpt_task/challenge
python -m venv .venv

# macOS / Linux
source .venv/bin/activate

# Windows
.venv\Scripts\activate

pip install -r requirements.txt
```

Requires **Node.js** for the MCP inspector (`npx`).

## Run

```bash
python -m task_scheduler
```

The process will hang waiting on stdin — this is correct (stdio MCP server).

## Verify with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python -m task_scheduler
```

In the browser GUI:

1. **Connect** — should show 4 tools: `task.create`, `task.list`, `task.status`, `task.cancel`
2. **task.create** — `description="Summarize tech news"`, `scheduled_at="2025-01-01T00:00:00"` (past time) — response: `job_id: 1`, status `pending`
3. Wait ~10 seconds, then **task.status** — `job_id: 1` — status should be `completed`
4. **task.create** — `scheduled_at="2099-12-31T00:00:00"` (future time) — get `job_id: 2`
5. **task.cancel** — `job_id: 2` — status `cancelled`
6. **task.list** — see all jobs

## Architecture

```
User → MCP Tool Call → server.py → tools.py → storage.py (DB)
                                                    ↑
                       scheduler.py (watcher + queue + worker)
```

- `storage.py` — Job model, DB session, time bucket helper
- `scheduler.py` — Watcher loop, worker loop, in-memory queue, crash recovery
- `tools.py` — Tool handlers, definitions, registry, route dispatch
- `server.py` — MCP protocol wiring, DB session lifecycle
