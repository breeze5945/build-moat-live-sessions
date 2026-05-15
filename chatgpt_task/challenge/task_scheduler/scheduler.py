import queue
import threading
import time
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .storage import Job, SessionLocal, compute_time_bucket, utcnow

job_queue: queue.Queue[int] = queue.Queue()


def recover_stuck_jobs(db: Session) -> None:
    stuck = db.query(Job).filter(Job.status.in_(["queued", "running"])).all()
    for job in stuck:
        job.status = "pending"
    if stuck:
        db.commit()


def rebucket_overdue_jobs(now: datetime, db: Session) -> None:
    """Re-bucket pending jobs whose time_bucket is older than the current window.

    Jobs scheduled far in the past get their bucket reset to the current hour so
    the watcher's normal bucket-based query can pick them up. This is the standard
    catch-up mechanism for jobs that were never processed (e.g., server was down).
    """
    current_bucket = compute_time_bucket(now)
    previous_bucket = compute_time_bucket(now - timedelta(hours=1))
    overdue = (
        db.query(Job)
        .filter(
            Job.scheduled_at <= now,
            Job.status == "pending",
            Job.time_bucket.notin_([current_bucket, previous_bucket]),
        )
        .all()
    )
    for job in overdue:
        job.time_bucket = current_bucket
    if overdue:
        db.commit()


def find_due_jobs(now: datetime, db: Session) -> list[Job]:
    current_bucket = compute_time_bucket(now)
    previous_bucket = compute_time_bucket(now - timedelta(hours=1))
    return (
        db.query(Job)
        .filter(
            Job.time_bucket.in_([current_bucket, previous_bucket]),
            Job.scheduled_at <= now,
            Job.status.in_(["pending", "queued"]),
        )
        .all()
    )


def watcher_loop(interval: int = 10) -> None:
    while True:
        db = SessionLocal()
        try:
            now = utcnow()
            rebucket_overdue_jobs(now, db)
            due_jobs = find_due_jobs(now, db)
            for job in due_jobs:
                if job.status == "pending":
                    job.status = "queued"
                    db.commit()
                job_queue.put(job.id)
        finally:
            db.close()
        time.sleep(interval)


def worker_loop() -> None:
    while True:
        job_id = job_queue.get()
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.id == job_id).first()
            if job is None or job.status == "cancelled":
                continue

            job.status = "running"
            db.commit()

            job.result = f"Executed: {job.description}"
            job.status = "completed"
            db.commit()
        except Exception as e:
            if job is not None:
                job.status = "failed"
                job.result = str(e)
                db.commit()
        finally:
            db.close()
            job_queue.task_done()


def start_scheduler() -> None:
    db = SessionLocal()
    try:
        recover_stuck_jobs(db)
    finally:
        db.close()

    watcher = threading.Thread(target=watcher_loop, daemon=True)
    worker = threading.Thread(target=worker_loop, daemon=True)
    watcher.start()
    worker.start()
