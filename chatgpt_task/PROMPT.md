# ChatGPT Task Scheduler Prototype

## System Requirements

Build a job scheduler with an MCP (Model Context Protocol) interface:
- Users schedule tasks for future execution via MCP tool calls
- A background watcher scans for due jobs and pushes them to a queue
- Workers pull jobs from the queue and execute them
- Support task creation, listing, status checking, and cancellation
- Tool naming follows namespace + action verb pattern (e.g., `task.create`)

### Architecture

```
User → MCP Tool Call → Job Scheduler API → DB
                                            ↓
                              Watcher (scans DB) → Queue → Worker (executes)
```

## Design Questions

Answer these before you start coding:

1. **Watcher vs Cron:** Why separate the watcher from the worker? What problems does a single cron job that both scans and executes have?

   **Answer:** A single cron that both scans and executes has four problems: (1) a slow job blocks the next scan cycle — if execution takes 90s, all subsequent jobs are delayed; (2) scan frequency is tied to execution time — you can't poll every 5s if execution averages 20s; (3) you can't scale independently — scanning is light IO (one SELECT) while execution may be heavy CPU/network (calling an LLM), but they're forced to scale together; (4) crash blast radius is large — one bad job kills the entire process, taking future scans down with it. Separating them lets the watcher stay a lightweight, stateless poller while workers can be scaled horizontally and fail independently.

   Note: scaling the watcher itself requires care — two watchers scanning the same bucket will enqueue duplicates. Solutions include partition assignment, DB locking, or idempotent consumption on the worker side. In practice, the watcher rarely needs scaling (it only does SELECT + UPDATE); it's the workers that need horizontal scale.

2. **Queue Layer:** Why put a queue between the watcher and worker instead of having the watcher call the worker directly? What are the benefits?

   **Answer:** The queue decouples production from consumption: (1) it buffers traffic spikes — 500 due jobs flood in, the queue absorbs them, workers drain at their own pace; (2) the watcher doesn't need to know how many workers exist or where they are — adding workers requires zero changes to watcher code; (3) crash recovery — if a worker dies, the job stays in the queue (SQS has visibility timeout, RabbitMQ has nack/requeue); (4) swapping the queue implementation doesn't affect business logic — prototype uses in-memory `queue.Queue`, production switches to SQS/Redis Streams by changing only the queue interface.

   Note: an in-memory `queue.Queue` provides no crash recovery — process death clears all memory. However, in this design the DB status field is the true source of truth (`pending → queued → running → completed`). On restart, the watcher re-scans the DB and picks up any jobs stuck in `pending` or `queued`. For small-scale persistence, a JSON file (append on enqueue, remove on dequeue — essentially a hand-rolled WAL) works. At medium scale, the DB itself acts as the queue via the status column. At large scale, a dedicated message broker (Redis Streams / SQS / RabbitMQ) provides built-in persistence, consumer acknowledgment, and retry.

3. **Time Bucket Partitioning:** Instead of `SELECT * WHERE scheduled_at <= now()`, why partition jobs by time bucket (e.g., hour)? What happens to query performance at 1M+ jobs without partitioning?

   **Answer:** `WHERE scheduled_at <= now()` is a range query. As time passes, more and more rows satisfy it (every past job qualifies). With 1M+ jobs, the DB walks the `scheduled_at` index to pull ~990K rows, then filters for `status = 'pending'` to find maybe 5 — scanning 990K rows every 10 seconds just to find 5. Switching to `WHERE time_bucket = '2026051114' AND status = 'pending'` turns this into a point lookup on the composite index `(time_bucket, status)`. The DB jumps directly to the current hour's pending jobs — scan size is O(bucket_size) regardless of total table size.

   | | Range query `<= now()` | Equality query `= bucket` |
   |---|---|---|
   | Scan scope | Grows with time, O(N) | Fixed, O(bucket_size) |
   | Index utilization | Low (range scan) | High (point lookup) |
   | At 1M rows | Scans hundreds of thousands | Scans tens of rows |

   Caveat — hour boundary problem: when the watcher crosses an hour boundary, pending jobs from the previous bucket get orphaned. Fix: query both the current and previous bucket (`time_bucket IN (current, previous)`). The cost of scanning one extra bucket (tens of rows) is negligible, but it guarantees no jobs are missed.

4. **Tool Naming:** Why `task.create` instead of `createTask`? How does naming convention affect LLM tool selection accuracy?

   **Answer:** The `namespace.action` format enables two-stage decision-making for LLMs: first narrow by namespace (is the user talking about `task`, `email`, or `user`?), then pick the action (`create`, `list`). This is more efficient than scanning flat names like `createTask`, `listTasks`, `sendEmail` where the LLM must parse each string's semantics individually. At 4 tools the difference is negligible; at 20+ tools across multiple domains, the namespace provides a natural grouping that improves selection accuracy. It also helps MCP client UIs display tools in logical groups.

   Note: dot notation is an MCP tool naming convention, not a code naming convention. In code, handlers are still named `handle_create_task` — only the externally exposed tool name uses `task.create`.

5. **Registry vs If-Else:** Why use a dictionary registry to route tool calls instead of if-else chains? What happens when you need to add the 20th tool?

   **Answer:** Three levels of difference: (1) **Open/Closed Principle** — adding a tool to if-elif means modifying the route function body; with a registry, you add one dict entry and route never changes. The cost of the 20th tool equals the cost of the 5th. (2) **Testability** — the registry can be cross-checked against TOOL_DEFINITIONS (`assert tool.name in REGISTRY`), catching missing registrations at test time. A forgotten elif silently falls through to the else branch. (3) **Runtime dynamic registration** — `REGISTRY[name] = handler` works at runtime; you can't add an elif branch at runtime. This matters for MCP's plugin architecture.

   The core insight: if-elif mixes "which tools exist" with "how to dispatch" in one function. A registry separates these two concerns so they can evolve independently — the same decoupling principle as separating watcher from worker in Q1.

## Verification

Your prototype is a real MCP server. Test it with the MCP inspector — no Claude needed.

### 1. Start the server (sanity check)

```bash
python -m app.mcp_server
```

The process should hang waiting on stdin (it's a stdio MCP server — that's correct). Ctrl+C to stop. If you see an `ImportError` or other crash, fix that first.

### 2. Run the MCP inspector

Requires Node.js (uses `npx`).

```bash
npx @modelcontextprotocol/inspector python -m app.mcp_server
```

This opens a browser GUI (usually `http://localhost:5173`).

Steps in the GUI:

1. Click **Connect** -> should show 4 tools: `task.create`, `task.list`, `task.status`, `task.cancel`
2. **task.create** -> fill `description="Summarize tech news"`, `scheduled_at="2025-01-01T00:00:00"` (past time so watcher picks it up immediately) -> **Run Tool** -> response should include `{"job_id": 1, "status": "pending", ...}`
3. Wait ~10 seconds, then **task.status** -> `job_id: 1` -> status should now be `"completed"`
4. **task.create** with future time `"2099-12-31T00:00:00"` -> get `job_id: 2`
5. **task.cancel** -> `job_id: 2` -> status `"cancelled"`
6. **task.list** -> see all your jobs

### 3. (Optional) Connect to Claude Desktop / Claude Code

Once the inspector tests pass, the server is ready. To talk to it through Claude:

**Claude Desktop**: edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) and add (use absolute paths):

```json
{
  "mcpServers": {
    "task-scheduler": {
      "command": "/absolute/path/to/scaffold/.venv/bin/python",
      "args": ["-m", "app.mcp_server"],
      "cwd": "/absolute/path/to/scaffold"
    }
  }
}
```

Restart Claude Desktop fully. The 🔨 icon in the chat input should show 4 tools.

**Claude Code**: edit `~/.claude.json` (top-level `mcpServers` for user scope) with the same block, or run `claude mcp add` from inside `scaffold/`.

Then chat:
> "Schedule a task to review PR #123 tomorrow at 9am."
> -> Claude calls `task.create` -> returns job_id
> "What's the status of that task?"
> -> Claude calls `task.status`

## Suggested Tech Stack

Python + the official `mcp` SDK is recommended (already in `requirements.txt` for the Guided Track). Challenge Track may use any language with an MCP SDK.
