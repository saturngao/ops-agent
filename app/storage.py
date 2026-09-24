"""数据存储（MySQL）：事件 ops_events + 指标 ops_metrics"""
import json
import time
import threading
import uuid
from typing import Any

from . import db

_lock = threading.Lock()


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False) if v is not None else "null"


def _unjson(v):
    if v is None:
        return None
    try:
        return json.loads(v)
    except Exception:
        return None


def _row_to_event(r) -> dict:
    return {
        "id": r["id"],
        "created_at": r["created_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("created_at") else "",
        "finished_at": r["finished_at"].strftime("%Y-%m-%d %H:%M:%S") if r.get("finished_at") else "",
        "subject": r.get("subject") or "",
        "from": r.get("sender") or "",
        "body": r.get("body") or "",
        "source": r.get("source") or "",
        "status": r.get("status") or "",
        "parsed": _unjson(r.get("parsed")),
        "triage": _unjson(r.get("triage")),
        "plan": _unjson(r.get("plan")),
        "results": _unjson(r.get("results")) or [],
        "trace": _unjson(r.get("trace")) or [],
        "escalation": _unjson(r.get("escalation")),
        "summary": r.get("summary") or "",
        "error": r.get("error") or "",
        "resolved": bool(r.get("resolved")),
    }


# ---------------- 事件存储 ----------------

def save_event(event: dict) -> dict:
    with _lock:
        db.ensure_init()
        if not event.get("id"):
            event["id"] = uuid.uuid4().hex[:12]
        if not event.get("created_at"):
            event["created_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        db._insert_event(event)
        return event


def list_events(limit: int = 100) -> list:
    db.ensure_init()
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ops_events ORDER BY created_at DESC, id DESC LIMIT %s",
                    (min(limit, 300),))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    return [_row_to_event(r) for r in rows]


def get_event(event_id: str) -> dict | None:
    db.ensure_init()
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM ops_events WHERE id=%s", (event_id,))
        r = cur.fetchone()
    if not r:
        return None
    cols = [d[0] for d in cur.description]
    return _row_to_event(dict(zip(cols, r)))


def update_event(event_id: str, patch: dict) -> dict | None:
    with _lock:
        db.ensure_init()
        e = get_event(event_id)
        if not e:
            return None
        e.update(patch)
        db._insert_event(e)  # REPLACE INTO 全量更新
        return e


# ---------------- 指标存储 ----------------

def append_metric(record: dict) -> None:
    record.setdefault("ts", time.time())
    db.ensure_init()
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO ops_metrics (ts, cpu_pct, mem_used_pct, disk_used_pct, load1, source) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (record["ts"], record.get("cpu_pct"), record.get("mem_used_pct"),
             record.get("disk_used_pct"), record.get("load1"), record.get("source")))


def list_metrics(hours: float = 24, limit: int = 5000) -> list:
    db.ensure_init()
    since = time.time() - hours * 3600
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT ts, cpu_pct, mem_used_pct, disk_used_pct, load1, source FROM ops_metrics "
            "WHERE ts >= %s ORDER BY ts ASC LIMIT %s", (since, limit))
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    # round floats（MySQL FLOAT 有精度噪音）
    for r in rows:
        for k in ("cpu_pct", "mem_used_pct", "disk_used_pct", "load1"):
            if isinstance(r.get(k), float):
                r[k] = round(r[k], 2)
    return rows


def metrics_summary(hours: float = 24) -> dict:
    """给 LLM 用的紧凑摘要：最新值 + 均值 + 峰值。"""
    rows = list_metrics(hours=hours, limit=3000)
    if not rows:
        return {"count": 0}
    keys = ["cpu_pct", "mem_used_pct", "disk_used_pct", "load1"]
    summary: dict[str, Any] = {
        "count": len(rows),
        "window_hours": hours,
        "first_ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(rows[0]["ts"])),
        "last_ts": time.strftime("%Y-%m-%d %H:%M", time.localtime(rows[-1]["ts"])),
        "latest": rows[-1],
    }
    stats = {}
    for k in keys:
        vals = [r[k] for r in rows if isinstance(r.get(k), (int, float))]
        if vals:
            stats[k] = {
                "avg": round(sum(vals) / len(vals), 2),
                "max": round(max(vals), 2),
                "min": round(min(vals), 2),
                "last": round(vals[-1], 2),
            }
    summary["stats"] = stats
    return summary
