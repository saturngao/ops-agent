"""MySQL 数据库层：连接管理、自动建库建表、旧文件数据迁移

连接参数来自本地 data/config.json 的 mysql 段（作为引导配置，
其余所有配置/事件/指标/LLM调用信息均存入 MySQL）。
"""
import json
import logging
import os
import threading
import time

import pymysql

from .config import DATA_DIR, get_mysql_config

log = logging.getLogger("ops-agent.db")

_local = threading.local()          # 每线程一个连接
_inited = False
_init_lock = threading.Lock()

DB_NAME_PLACEHOLDER = "__db__"


def _conn_params(db: str | None = None) -> dict:
    c = get_mysql_config()
    return {
        "host": c.get("host", "127.0.0.1"),
        "port": int(c.get("port", 3306)),
        "user": c.get("user", "root"),
        "password": c.get("password", ""),
        "database": db if db is not None else c.get("database", "ops_agent"),
        "charset": "utf8mb4",
        "autocommit": True,
        "connect_timeout": 5,
    }


def get_conn() -> pymysql.connections.Connection:
    """获取当前线程的 MySQL 连接（自动重连）。"""
    conn = getattr(_local, "conn", None)
    try:
        if conn is not None:
            conn.ping(reconnect=True)
            return conn
    except Exception:
        conn = None
    try:
        conn.close()
    except Exception:
        pass
    conn = pymysql.connect(**_conn_params())
    _local.conn = conn
    return conn


def ensure_init() -> None:
    """幂等初始化：建库建表（进程内只执行一次，失败可重试）。"""
    global _inited
    if _inited:
        return
    with _init_lock:
        if _inited:
            return
        c = get_mysql_config()
        # 1. 建库
        base = pymysql.connect(host=c.get("host", "127.0.0.1"), port=int(c.get("port", 3306)),
                               user=c.get("user", "root"), password=c.get("password", ""),
                               charset="utf8mb4", autocommit=True, connect_timeout=5)
        try:
            with base.cursor() as cur:
                cur.execute(f"CREATE DATABASE IF NOT EXISTS `{c.get('database', 'ops_agent')}` "
                            "DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        finally:
            base.close()
        # 2. 建表
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ops_config (
                    id TINYINT PRIMARY KEY,
                    data LONGTEXT NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ops_events (
                    id VARCHAR(16) PRIMARY KEY,
                    created_at DATETIME,
                    finished_at DATETIME NULL,
                    subject TEXT,
                    sender VARCHAR(255),
                    body MEDIUMTEXT,
                    source VARCHAR(32),
                    status VARCHAR(16),
                    parsed LONGTEXT, triage LONGTEXT, plan LONGTEXT,
                    results LONGTEXT, trace LONGTEXT, escalation LONGTEXT,
                    summary TEXT, error TEXT,
                    resolved TINYINT DEFAULT 0,
                    INDEX idx_created (created_at),
                    INDEX idx_status (status)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ops_metrics (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    ts DOUBLE NOT NULL,
                    cpu_pct FLOAT NULL, mem_used_pct FLOAT NULL,
                    disk_used_pct FLOAT NULL, load1 FLOAT NULL,
                    source VARCHAR(16),
                    INDEX idx_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
            cur.execute("""
                CREATE TABLE IF NOT EXISTS ops_llm_calls (
                    id BIGINT AUTO_INCREMENT PRIMARY KEY,
                    ts DATETIME DEFAULT CURRENT_TIMESTAMP,
                    node VARCHAR(64),
                    model VARCHAR(64),
                    ok TINYINT DEFAULT 1,
                    latency_ms INT DEFAULT 0,
                    prompt_chars INT DEFAULT 0,
                    error VARCHAR(500),
                    INDEX idx_ts (ts)
                ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""")
        _inited = True
        log.info("MySQL 初始化完成（库: %s）", c.get("database", "ops_agent"))


def _json(v) -> str:
    return json.dumps(v, ensure_ascii=False) if v is not None else "null"


def _unjson(v):
    if v is None:
        return None
    try:
        return json.loads(v)
    except Exception:
        return None


# ---------------- 旧文件数据迁移 ----------------

def migrate_legacy() -> dict:
    """把旧的 events.json / metrics.jsonl 导入 MySQL（成功后重命名保留）。"""
    ensure_init()
    moved = {"events": 0, "metrics": 0}
    ev_path = os.path.join(DATA_DIR, "events.json")
    if os.path.exists(ev_path):
        try:
            with open(ev_path, "r", encoding="utf-8") as f:
                events = json.load(f)
            for e in reversed(events):  # 旧→新插入
                _insert_event(e)
            moved["events"] = len(events)
            os.replace(ev_path, ev_path + ".imported")
        except Exception as e:
            log.warning("迁移 events.json 失败: %s", e)
    mt_path = os.path.join(DATA_DIR, "metrics.jsonl")
    if os.path.exists(mt_path):
        try:
            rows = []
            with open(mt_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            rows.append(json.loads(line))
                        except Exception:
                            pass
            if rows:
                conn = get_conn()
                with conn.cursor() as cur:
                    cur.executemany(
                        "INSERT INTO ops_metrics (ts, cpu_pct, mem_used_pct, disk_used_pct, load1, source) "
                        "VALUES (%s,%s,%s,%s,%s,%s)",
                        [(r.get("ts", time.time()), r.get("cpu_pct"), r.get("mem_used_pct"),
                          r.get("disk_used_pct"), r.get("load1"), r.get("source")) for r in rows])
            moved["metrics"] = len(rows)
            os.replace(mt_path, mt_path + ".imported")
        except Exception as e:
            log.warning("迁移 metrics.jsonl 失败: %s", e)
    if moved["events"] or moved["metrics"]:
        log.info("旧文件数据迁移完成: %s", moved)
    return moved


def _insert_event(e: dict) -> None:
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(
            "REPLACE INTO ops_events (id, created_at, finished_at, subject, sender, body, source, status,"
            " parsed, triage, plan, results, trace, escalation, summary, error, resolved)"
            " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (e.get("id"), e.get("created_at"), e.get("finished_at"), e.get("subject", ""),
             e.get("from", ""), e.get("body", ""), e.get("source", ""), e.get("status", ""),
             _json(e.get("parsed")), _json(e.get("triage")), _json(e.get("plan")),
             _json(e.get("results")), _json(e.get("trace")), _json(e.get("escalation")),
             e.get("summary", ""), e.get("error", ""), 1 if e.get("resolved") else 0))


# ---------------- LLM 调用日志 ----------------

def log_llm_call(node: str, model: str, ok: bool, latency_ms: int, prompt_chars: int, error: str = "") -> None:
    try:
        ensure_init()
        conn = get_conn()
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ops_llm_calls (node, model, ok, latency_ms, prompt_chars, error) VALUES (%s,%s,%s,%s,%s,%s)",
                (node, model, 1 if ok else 0, latency_ms, prompt_chars, error[:500]))
    except Exception as e:
        log.warning("记录 LLM 调用日志失败: %s", e)


def list_llm_calls(limit: int = 100) -> list:
    ensure_init()
    conn = get_conn()
    with conn.cursor(pymysql.cursors.DictCursor) as cur:
        cur.execute("SELECT * FROM ops_llm_calls ORDER BY id DESC LIMIT %s", (min(limit, 300),))
        rows = cur.fetchall()
    for r in rows:
        r["ts"] = r["ts"].strftime("%Y-%m-%d %H:%M:%S") if r.get("ts") else ""
        r["ok"] = bool(r["ok"])
    return rows
