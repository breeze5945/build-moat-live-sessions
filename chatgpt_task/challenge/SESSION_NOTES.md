# ChatGPT Task Scheduler — Session Notes

## 決策摘要

| 項目 | 選擇 |
|------|------|
| Track | Challenge Track（從零建，不填 scaffold TODO） |
| 目錄 | `chatgpt_task/challenge/`（與 scaffold/ 平行） |
| Stack | Python + 官方 mcp SDK + SQLAlchemy + SQLite |
| Design Questions | 直接寫進 PROMPT.md |
| 驗證方式 | MCP inspector 端到端（全部 pass） |

## 最終目錄結構

```
chatgpt_task/challenge/
├── README.md
├── requirements.txt           # mcp>=1.0.0, sqlalchemy==2.0.36
├── run.py                     # 絕對路徑 entry point（inspector 用）
├── .gitignore
└── task_scheduler/
    ├── __init__.py
    ├── __main__.py            # python -m task_scheduler 入口
    ├── server.py              # MCP server 協定 wiring + main()
    ├── tools.py               # Tool 定義 + handlers + TOOL_REGISTRY + 派發
    ├── scheduler.py           # watcher/worker loop + queue + crash recovery
    └── storage.py             # Job model + Session + compute_time_bucket
```

## 架構決策（2026-05-14 討論確認）

### 決策一：server.py 不直接呼叫 storage
server 只做三件事：接收 MCP request、開/關 DB session、呼叫 `route_tool_call`。
實際業務邏輯（CRUD）全在 tools.py 裡，server 完全不知道裡面在做什麼。

### 決策二：scheduler 和 tools 不互相依賴
```
tools.py ──→ storage.py ←── scheduler.py
```
兩條路徑獨立演化：tools 新增 handler 不影響 scheduler，scheduler 換 queue 不影響 tools。
未來 worker 執行邏輯變複雜時，會從 scheduler 長出 `executor.py`，不破壞現有結構。

### 決策三：queue 留在 scheduler.py，不獨立 queue.py
scheduler 的職責就是「掃描 + 排隊 + 執行」，queue 是這三步的黏合劑。
當需要跨 process 的 queue（Redis/SQS）時整個 scheduler 都會大改，現在預先拆 queue.py 沒有實際好處。

## 實作中發現的 Bug 與修法

### Bug：過期 time_bucket 的 job 永遠不被掃到

**根因：** `task.create` 用 `scheduled_at` 算出 `time_bucket`（如 `"2025010100"`）。
watcher 只查 `current_bucket`（今天）和 `previous_bucket`（前一小時），
完全不會掃到 2025 年的舊 bucket，job 永遠卡在 `pending`。

**修法：** 新增 `rebucket_overdue_jobs()`，在每次 watcher 輪詢時先把
`scheduled_at <= now` 但 bucket 已過期的 pending jobs 的 `time_bucket` 更新成 current bucket。
這是生產系統的標準 **catch-up / backfill** 機制：系統停機重啟後，積壓 jobs 被重新帶進正常掃描視窗。

```python
def rebucket_overdue_jobs(now, db):
    current_bucket = compute_time_bucket(now)
    previous_bucket = compute_time_bucket(now - timedelta(hours=1))
    overdue = db.query(Job).filter(
        Job.scheduled_at <= now,
        Job.status == "pending",
        Job.time_bucket.notin_([current_bucket, previous_bucket]),
    ).all()
    for job in overdue:
        job.time_bucket = current_bucket
    if overdue:
        db.commit()
```

### 環境問題：Windows 長路徑
venv 建在 worktree 深層路徑下會遇到 Windows 路徑長度限制（ENAMETOOLONG）。
解法：venv 建在短路徑 `C:/tmp/ts-venv`，inspector 用絕對路徑指向 `run.py`。

```bash
python -m venv C:/tmp/ts-venv
C:/tmp/ts-venv/Scripts/pip install -r requirements.txt
npx @modelcontextprotocol/inspector@0.9.0 "C:/tmp/ts-venv/Scripts/python" "/absolute/path/to/challenge/run.py"
```

注意：Node.js v20 需使用 inspector v0.9.0，v0.21+ 要求 Node v22.7.5+。

## Design Questions 討論紀錄

### Q1: Watcher vs Cron
單一 cron 同時掃描 + 執行的問題：慢 job 卡住下一輪掃描、無法獨立 scale、crash blast radius 大。
拆開後 watcher 是無狀態輕量輪詢，worker 可水平擴展。
延伸：watcher scale up 需注意重複 enqueue 問題，實務上 watcher 不需要 scale，需要 scale 的是 worker。

### Q2: Queue Layer
解耦生產與消費：buffer 尖峰、生產者不需知道消費者、crash recovery、換 queue 實作不影響業務邏輯。
in-memory queue 沒有 crash recovery，DB status 欄位是真正的 source of truth。
持久化演進：JSON file（手寫 WAL）→ DB status 欄位 → Redis Streams / SQS。

### Q3: Time Bucket Partitioning
`scheduled_at <= now()` 是 range scan，隨時間成長 O(N)。
`time_bucket = 'current'` 是 point lookup，配合複合索引掃描固定 O(bucket_size)。
Hour boundary 問題：查兩個 bucket（current + previous）保證不漏。
過期 bucket 問題（本次 bug）：定期 rebucket 積壓 jobs 是生產系統的標準 catch-up 機制。

### Q4: Tool Naming
`task.create` 兩段式讓 LLM 分層決策（先 namespace → 再 action）。
20+ tools 時效果明顯；dot notation 只是 MCP 命名慣例，程式碼裡 handler 還是用 snake_case。

### Q5: Registry vs If-Else
三層差距：Open/Closed（新增只加一行 dict entry）、可測試性（`assert tool.name in REGISTRY`）、Runtime 動態註冊。
本質：把「有哪些 tool」和「怎麼 dispatch」拆開，跟 watcher/worker 拆法是同一個設計直覺。

## 實作進度

| 步驟 | 狀態 |
|------|------|
| Design Questions 答案寫進 PROMPT.md | ✅ |
| storage.py | ✅ |
| scheduler.py（含 rebucket_overdue_jobs）| ✅ |
| tools.py | ✅ |
| server.py | ✅ |
| __main__.py | ✅ |
| requirements.txt + README.md + .gitignore | ✅ |
| Verification — sanity check | ✅ |
| Verification — MCP inspector happy path | ✅ |
| Verification — 失敗路徑測試 | ✅ |

**全部完成。**
