from datetime import datetime
from typing import Callable

from mcp.types import Tool
from sqlalchemy.orm import Session

from .storage import Job, compute_time_bucket


def handle_create(db: Session, *, description: str, scheduled_at: str) -> dict:
    try:
        dt = datetime.fromisoformat(scheduled_at)
    except (ValueError, TypeError):
        return {"error": f"Invalid ISO 8601 datetime: {scheduled_at}"}
    job = Job(
        description=description,
        scheduled_at=dt,
        time_bucket=compute_time_bucket(dt),
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    return {"job_id": job.id, "status": job.status, "scheduled_at": str(job.scheduled_at)}


def handle_list(db: Session) -> dict:
    jobs = db.query(Job).order_by(Job.scheduled_at.desc()).all()
    return {
        "jobs": [
            {
                "job_id": j.id,
                "description": j.description,
                "status": j.status,
                "scheduled_at": str(j.scheduled_at),
            }
            for j in jobs
        ]
    }


def handle_status(db: Session, *, job_id: int) -> dict:
    try:
        job_id = int(job_id)
    except (ValueError, TypeError):
        return {"error": f"Invalid job_id: {job_id}"}
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        return {"error": f"Job {job_id} not found"}
    return {
        "job_id": job.id,
        "description": job.description,
        "status": job.status,
        "scheduled_at": str(job.scheduled_at),
        "result": job.result,
    }


def handle_cancel(db: Session, *, job_id: int) -> dict:
    try:
        job_id = int(job_id)
    except (ValueError, TypeError):
        return {"error": f"Invalid job_id: {job_id}"}
    job = db.query(Job).filter(Job.id == job_id).first()
    if job is None:
        return {"error": f"Job {job_id} not found"}
    if job.status in ("completed", "failed"):
        return {"error": f"Cannot cancel job in '{job.status}' state"}
    job.status = "cancelled"
    db.commit()
    return {"job_id": job.id, "status": "cancelled"}


TOOL_DEFINITIONS: list[Tool] = [
    Tool(
        name="task.create",
        description="Schedule a new task for future execution",
        inputSchema={
            "type": "object",
            "properties": {
                "description": {"type": "string", "description": "What the task should do"},
                "scheduled_at": {
                    "type": "string",
                    "format": "date-time",
                    "description": "When to run, ISO 8601 format (e.g. 2026-05-14T10:00:00)",
                },
            },
            "required": ["description", "scheduled_at"],
        },
    ),
    Tool(
        name="task.list",
        description="List all scheduled tasks",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="task.status",
        description="Get the status of a scheduled task by job_id",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "The job ID returned by task.create"},
            },
            "required": ["job_id"],
        },
    ),
    Tool(
        name="task.cancel",
        description="Cancel a scheduled task that hasn't completed yet",
        inputSchema={
            "type": "object",
            "properties": {
                "job_id": {"type": "integer", "description": "The job ID to cancel"},
            },
            "required": ["job_id"],
        },
    ),
]

TOOL_REGISTRY: dict[str, Callable] = {
    "task.create": handle_create,
    "task.list": handle_list,
    "task.status": handle_status,
    "task.cancel": handle_cancel,
}


def route_tool_call(tool_name: str, arguments: dict, db: Session) -> dict:
    handler = TOOL_REGISTRY.get(tool_name)
    if handler is None:
        return {"error": f"Unknown tool: {tool_name}"}
    return handler(db, **arguments)
