"""运维 Agent Web 应用入口：FastAPI + LangGraph + 邮件 MCP 服务"""
import logging
import threading
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import analysis, db, email_mcp, metrics_collector, ssh_client
from app.agent.graph import run_event
from app.config import load_config, save_config, redacted
from app.storage import list_events, get_event, list_metrics

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
log = logging.getLogger("ops-agent")

runtime = {
    "email_last_poll": None,
    "email_last_result": None,
    "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
}


# ---------------- 邮件轮询线程（邮件 MCP 服务） ----------------

def email_poll_loop():
    while True:
        cfg = load_config()
        interval = max(30, int(cfg["email"].get("poll_interval", 60)))
        try:
            if cfg["email"].get("enabled") and cfg["email"].get("username") and cfg["email"].get("auth_code"):
                mails = email_mcp.receive_emails(limit=10)
                runtime["email_last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S")
                runtime["email_last_result"] = f"收到 {len(mails)} 封运维邮件"
                for m in mails:
                    log.info("收到运维邮件: %s", m["subject"])
                    run_event({"subject": m["subject"], "from": m["from"],
                               "body": m["body"], "source": "email"})
        except Exception as e:
            log.warning("邮件轮询异常: %s", e)
            runtime["email_last_result"] = f"轮询异常: {e}"
        time.sleep(interval)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 初始化 MySQL（自动建库建表）并迁移旧文件数据
    try:
        db.ensure_init()
        moved = db.migrate_legacy()
        if moved["events"] or moved["metrics"]:
            log.info("已迁移旧文件数据到 MySQL: %s", moved)
    except Exception as e:
        log.error("MySQL 初始化失败: %s（请检查 MySQL 服务与 data/config.json 中 mysql 配置）", e)
    metrics_collector.start()
    threading.Thread(target=email_poll_loop, name="email-poller", daemon=True).start()
    log.info("运维 Agent 已启动")
    yield


app = FastAPI(title="运维 Agent", lifespan=lifespan)


# ---------------- API ----------------

@app.get("/api/status")
def api_status():
    cfg = load_config()
    events = list_events(limit=500)
    return {
        "runtime": {
            **runtime,
            "collector": metrics_collector.last_run,
        },
        "email": email_mcp.email_status(),
        "ssh": {
            "enabled": bool(cfg["ssh"].get("enabled")),
            "configured": bool(cfg["ssh"].get("host")),
            "connected": ssh_client.ssh_enabled(),
        },
        "llm": {
            "configured": bool(cfg["llm"].get("base_url") and cfg["llm"].get("api_key")),
            "model": cfg["llm"].get("model"),
        },
        "counts": {
            "total": len(events),
            "handled": sum(1 for e in events if e.get("status") == "handled"),
            "escalated": sum(1 for e in events if e.get("status") == "escalated"),
            "ignored": sum(1 for e in events if e.get("status") == "ignored"),
        },
    }


@app.get("/api/config")
def api_get_config():
    return redacted(load_config())


class ConfigBody(BaseModel):
    config: dict


@app.post("/api/config")
def api_set_config(body: ConfigBody):
    patch = body.config
    # 前端传回掩码时不覆盖真实密钥
    cur = load_config()
    for section in ("llm", "email", "ssh", "mysql"):
        for field in ("api_key", "auth_code", "password"):
            v = (patch.get(section, {}) or {}).get(field)
            if v in ("******", None, ""):
                patch.setdefault(section, {})
                if v == "******":
                    patch[section][field] = cur.get(section, {}).get(field, "")
                else:
                    patch[section].pop(field, None)
    cfg = save_config(patch)
    return {"ok": True, "config": redacted(cfg)}


@app.get("/api/events")
def api_events(limit: int = 100):
    return list_events(limit=min(limit, 300))


@app.get("/api/events/{event_id}")
def api_event_detail(event_id: str):
    e = get_event(event_id)
    if not e:
        raise HTTPException(404, "事件不存在")
    return e


@app.post("/api/events/{event_id}/rerun")
def api_event_rerun(event_id: str):
    e = get_event(event_id)
    if not e:
        raise HTTPException(404, "事件不存在")
    ev = {"subject": e.get("subject", ""), "from": e.get("from", ""),
          "body": e.get("body", ""), "source": e.get("source", "manual") + "-rerun"}
    threading.Thread(target=run_event, args=(ev,), daemon=True).start()
    return {"ok": True, "message": "已重新提交 Agent 处理"}


class SimulateBody(BaseModel):
    subject: str = "【告警】磁盘使用率超过 90%"
    body: str = ("监控告警：服务器 / 分区磁盘使用率达到 92%，超过阈值 90%。"
                 "建议检查大文件并清理日志。主机: web-server-01")


@app.post("/api/events/simulate")
def api_simulate(body: SimulateBody):
    """注入一封模拟告警邮件，走完整 Agent 流程（用于无邮箱配置时体验）。"""
    ev = {"subject": body.subject, "from": "monitor@demo.local",
          "body": body.body, "source": "simulate"}
    threading.Thread(target=run_event, args=(ev,), daemon=True).start()
    return {"ok": True, "message": "已注入模拟邮件，Agent 正在处理"}


@app.post("/api/email/check")
def api_email_check():
    mails = email_mcp.receive_emails(limit=10)
    runtime["email_last_poll"] = time.strftime("%Y-%m-%d %H:%M:%S")
    for m in mails:
        threading.Thread(target=run_event, args=({"subject": m["subject"], "from": m["from"],
                                                  "body": m["body"], "source": "email"},),
                         daemon=True).start()
    return {"ok": True, "received": len(mails)}


@app.get("/api/metrics")
def api_metrics(hours: float = 24):
    return {"hours": hours, "records": list_metrics(hours=hours)}


class AnalysisBody(BaseModel):
    question: str
    hours: float = 24


@app.post("/api/analysis")
def api_analysis(body: AnalysisBody):
    return analysis.analyze(body.question, hours=body.hours)


@app.post("/api/ssh/test")
def api_ssh_test():
    return ssh_client.test_connection()


@app.get("/api/llm-logs")
def api_llm_logs(limit: int = 100):
    return db.list_llm_calls(limit=limit)


@app.post("/api/db/test")
def api_db_test():
    """测试 MySQL 连接（使用当前本地引导配置）。"""
    try:
        db.ensure_init()
        conn = db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT VERSION()")
            version = cur.fetchone()[0]
        return {"ok": True, "message": f"MySQL 连接成功（版本 {version}）"}
    except Exception as e:
        return {"ok": False, "message": f"MySQL 连接失败: {e}"}


# ---------------- 静态页面 ----------------

app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
def index():
    return FileResponse("static/index.html")


if __name__ == "__main__":
    import os
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
