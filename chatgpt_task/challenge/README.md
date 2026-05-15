# ChatGPT Task Scheduler — Challenge Track

A complete MCP (Model Context Protocol) stdio server built from scratch. Users schedule tasks via natural language through Claude, which automatically calls the appropriate tool.

```
"Schedule a task to review PR #123 tomorrow at 9am."
  -> Claude calls task.create -> returns job_id
"What's the status of that task?"
  -> Claude calls task.status -> returns status
```

## Architecture

```
User -> MCP Tool Call -> server.py -> tools.py -> storage.py (DB)
                                                      |
                          scheduler.py (watcher + queue + worker)
                                |
                          storage.py (DB)
```

### Why this structure?

The system has four modules, each with a single responsibility:

| Module | Responsibility | Depends on |
|--------|---------------|------------|
| `storage.py` | Data layer: Job model, DB session, time bucket computation | nothing |
| `scheduler.py` | Concurrency: watcher loop, worker loop, in-memory queue, crash recovery | storage |
| `tools.py` | Business logic: 4 CRUD handlers, tool definitions, registry dispatch | storage |
| `server.py` | Protocol: MCP wiring, DB session lifecycle, async bridge | tools, scheduler |

Key design decisions:

- **server.py never touches storage directly.** It opens/closes DB sessions, but all business logic lives in tools.py. This keeps protocol wiring decoupled from domain logic.
- **scheduler.py and tools.py don't depend on each other.** Both depend on storage.py, but neither knows the other exists. They can evolve independently.
- **Queue stays inside scheduler.py.** The queue is the glue between watcher and worker, both of which live in scheduler. Extracting a separate queue.py adds complexity without benefit at this scale.

### Why separate watcher from worker? (vs single cron)

A single cron that both scans and executes has compounding problems:
- A slow job (90s execution) blocks the next scan cycle, delaying all subsequent jobs
- Scan frequency is tied to execution time -- can't poll every 5s if execution averages 20s
- Can't scale independently -- scanning is light IO, execution may be heavy CPU/network
- Crash blast radius is large -- one bad job kills scanning too

Separating them lets the watcher stay a lightweight poller (one SELECT per cycle) while workers can be scaled horizontally and fail independently.

### Why put a queue between watcher and worker?

- **Buffer traffic spikes** -- 500 due jobs flood in, queue absorbs, workers drain at their own pace
- **Decoupled scaling** -- watcher doesn't know how many workers exist; adding workers requires zero watcher changes
- **Crash recovery** -- worker dies, job stays in queue (in production: SQS visibility timeout, RabbitMQ nack)
- **Swappable** -- prototype uses in-memory `queue.Queue`, production switches to SQS/Redis Streams by changing only the queue interface

Note: in-memory queue has no crash recovery. The DB status field (`pending -> queued -> running -> completed`) is the true source of truth. On restart, `recover_stuck_jobs()` resets any stuck jobs back to `pending`.

### Why time bucket partitioning? (vs `WHERE scheduled_at <= now()`)

`scheduled_at <= now()` is a range query. At 1M+ jobs, it scans every past job (990K rows) just to find 5 pending ones, every 10 seconds.

Time bucket converts this to a point lookup:

```sql
-- Range scan: O(N), gets worse over time
SELECT * FROM jobs WHERE scheduled_at <= now() AND status = 'pending'

-- Point lookup: O(bucket_size), constant regardless of table size
SELECT * FROM jobs WHERE time_bucket = '2026051514' AND status = 'pending'
```

The composite index `(time_bucket, status)` lets the DB jump directly to the current hour's pending jobs.

**Hour boundary problem:** When the watcher crosses an hour boundary, the previous bucket's late jobs get orphaned. Fix: query both current and previous bucket. Cost: scanning one extra bucket (tens of rows) is negligible.

**Overdue bucket problem:** Jobs scheduled far in the past (e.g., `2025-01-01`) have old buckets the watcher never queries. Fix: `rebucket_overdue_jobs()` runs each watcher cycle, re-bucketing overdue pending jobs to the current hour. This is a standard catch-up/backfill mechanism for production systems recovering from downtime.

### Why `task.create` naming? (vs `createTask`)

The `namespace.action` format enables two-stage LLM decision-making:
1. Narrow by namespace -- is the user talking about `task`, `email`, or `user`?
2. Pick the action -- `create`, `list`, `status`, `cancel`

At 4 tools the difference is negligible. At 20+ tools across multiple domains, namespace grouping measurably improves tool selection accuracy. MCP client UIs also display `task.*` tools as a logical group.

Note: dot notation is an MCP tool naming convention, not a code naming convention. Handlers in code are still `handle_create`, `handle_list`, etc.

### Why registry pattern? (vs if-elif chain)

```python
# If-elif: adding a tool means modifying the dispatch function
def route(name, args, db):
    if name == "task.create": return handle_create(db, **args)
    elif name == "task.list": return handle_list(db)
    # ... 18 more elifs ...

# Registry: adding a tool means adding one dict entry. route() never changes.
TOOL_REGISTRY = {
    "task.create": handle_create,
    "task.list":   handle_list,
    "task.status": handle_status,
    "task.cancel": handle_cancel,
}

def route(name, args, db):
    handler = TOOL_REGISTRY.get(name)
    return handler(db, **args) if handler else {"error": f"Unknown tool: {name}"}
```

Three advantages:
1. **Open/Closed Principle** -- route function is closed for modification, open for extension
2. **Testability** -- `assert tool.name in REGISTRY` catches missing registrations at test time
3. **Runtime registration** -- plugins can add tools dynamically; if-elif can't

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

**Windows long path note:** If venv creation fails due to path length, create it in a short path:
```bash
python -m venv C:\tmp\ts-venv
C:\tmp\ts-venv\Scripts\pip install -r requirements.txt
```

## Run

```bash
python -m task_scheduler
```

The process will hang waiting on stdin -- this is correct behavior for a stdio MCP server. Press Ctrl+C to stop.

## Verify with MCP Inspector

```bash
npx @modelcontextprotocol/inspector python -m task_scheduler
```

> Note: Node.js v20 requires inspector v0.9.0 (`npx @modelcontextprotocol/inspector@0.9.0`). v0.21+ requires Node v22.7.5+.

In the browser GUI (usually `http://localhost:5173` or `http://127.0.0.1:6274`):

### Happy path

1. **Connect** -- should show 4 tools: `task.create`, `task.list`, `task.status`, `task.cancel`
2. **task.create** -- `description="Summarize tech news"`, `scheduled_at="2025-01-01T00:00:00"` (past time) -- response: `job_id: 1`, status `pending`
3. Wait ~10 seconds, then **task.status** -- `job_id: 1` -- status should be `completed` (watcher picked it up via rebucket, worker executed it)
4. **task.create** -- `scheduled_at="2099-12-31T00:00:00"` (future time) -- get `job_id: 2`
5. **task.cancel** -- `job_id: 2` -- status `cancelled`
6. **task.list** -- see all jobs

### Failure paths

- **task.cancel** with `job_id: 1` (already completed) -- should return error
- **task.status** with `job_id: 9999` (doesn't exist) -- should return error
- **task.create** with `scheduled_at="not-a-date"` -- should return clear error message

## Connect to Claude Desktop

Edit `claude_desktop_config.json`:
- **macOS:** `~/Library/Application Support/Claude/claude_desktop_config.json`
- **Windows:** `%APPDATA%\Claude\claude_desktop_config.json`

Add to `mcpServers` (use absolute paths):

```json
{
  "mcpServers": {
    "task-scheduler": {
      "command": "/absolute/path/to/.venv/bin/python",
      "args": ["/absolute/path/to/challenge/run.py"]
    }
  }
}
```

Restart Claude Desktop fully. The tool icon should show 4 tools.

Then chat:
- "Schedule a task to review PR #123 tomorrow at 9am." -> Claude calls `task.create`
- "What's the status of that task?" -> Claude calls `task.status`
- "Cancel it." -> Claude calls `task.cancel`
- "List all tasks." -> Claude calls `task.list`

## Connect to Claude Code

```bash
claude mcp add task-scheduler -- /absolute/path/to/.venv/bin/python /absolute/path/to/challenge/run.py
```

## File Overview

```
challenge/
├── run.py                     # Absolute-path entry point (for Claude Desktop/inspector)
├── requirements.txt           # mcp>=1.0.0, sqlalchemy==2.0.36
├── .gitignore                 # .venv/, challenge.db, __pycache__/
└── task_scheduler/
    ├── __init__.py
    ├── __main__.py            # python -m task_scheduler entry point
    ├── storage.py             # Job model, DB engine/session, compute_time_bucket, init_db
    ├── scheduler.py           # watcher_loop, worker_loop, job_queue, recover/rebucket
    ├── tools.py               # handle_create/list/status/cancel, TOOL_REGISTRY, route
    └── server.py              # MCP Server wiring, async bridge, main()
```
