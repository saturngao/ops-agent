"""离线流程测试：用桩替换 LLM 与 SSH，验证 LangGraph 各条路径"""
import sys
sys.path.insert(0, ".")

from app import llm
from app import ssh_client


def test_escalate_path():
    """分诊判定无法处理 → 升级"""
    llm.chat_json = lambda msgs, temperature=None, node=None: (
        {"type": "磁盘告警", "severity": "critical", "service": "web-01",
         "summary": "磁盘满", "is_ops_event": True}
        if "解析器" in msgs[0]["content"] else
        {"auto_handle": False, "reason": "涉及数据删除，需人工"}
    )
    from importlib import reload
    from app.agent import graph
    reload(graph)
    r = graph.run_event({"subject": "【告警】磁盘 95%", "from": "m@x.com", "body": "磁盘满了", "source": "test"})
    assert r["status"] == "escalated", r
    print("✓ 升级路径 OK")


def test_handled_path():
    """分诊判定可处理 → 规划 → SSH执行 → 验证 → 记录"""
    seq = []
    def fake_json(msgs, temperature=None, node=None):
        s = msgs[0]["content"]
        seq.append(s[:20])
        if "解析器" in s:
            return {"type": "磁盘告警", "severity": "high", "service": "web-01",
                    "summary": "日志过大", "is_ops_event": True}
        if "分诊器" in s:
            return {"auto_handle": True, "reason": "可自动清理"}
        if "规划器" in s:
            return {"diagnosis": "日志过大", "commands": [
                {"cmd": "df -h", "purpose": "查看磁盘"},
                {"cmd": "rm -rf /", "purpose": "危险命令应被拦截"},
                {"cmd": "journalctl --disk-usage", "purpose": "查看日志占用"}]}
        if "验证器" in s:
            return {"success": True, "resolved": True, "summary": "已定位并缓解", "need_more": False}
        return {}
    llm.chat_json = fake_json
    ssh_client.ssh_enabled = lambda: True
    ssh_client.run_command = lambda cmd, timeout=None: (
        {"ok": False, "stdout": "", "stderr": "已拦截", "exit_code": -1}
        if cmd.startswith("rm") else {"ok": True, "stdout": "/ 92% used", "stderr": "", "exit_code": 0})
    from importlib import reload
    from app.agent import graph
    reload(graph)
    r = graph.run_event({"subject": "【告警】磁盘 92%", "from": "m@x.com", "body": "磁盘告警", "source": "test"})
    assert r["status"] == "handled", r
    evs = [e for e in __import__("app.storage", fromlist=["list_events"]).list_events(10)
           if e["subject"] == "【告警】磁盘 92%"]
    assert evs, "未找到已处理事件"
    e = evs[0]
    assert len(e["results"]) == 3 and e["results"][1]["ok"] is False, "危险命令应被拦截"
    print("✓ 自动处理路径 OK（含危险命令拦截）")


def test_retry_then_escalate():
    """验证不通过且需要重试 → 第二轮仍失败 → 升级"""
    calls = {"verify": 0}
    def fake_json(msgs, temperature=None, node=None):
        s = msgs[0]["content"]
        if "解析器" in s:
            return {"type": "服务宕机", "severity": "critical", "service": "nginx", "summary": "nginx down", "is_ops_event": True}
        if "分诊器" in s:
            return {"auto_handle": True, "reason": "可重启"}
        if "规划器" in s:
            return {"diagnosis": "nginx down", "commands": [{"cmd": "systemctl restart nginx", "purpose": "重启"}]}
        if "验证器" in s:
            calls["verify"] += 1
            return {"success": False, "resolved": False, "summary": "重启失败", "need_more": calls["verify"] < 2}
        return {}
    llm.chat_json = fake_json
    ssh_client.ssh_enabled = lambda: True
    ssh_client.run_command = lambda cmd, timeout=None: {"ok": False, "stdout": "", "stderr": "Job failed", "exit_code": 1}
    from importlib import reload
    from app.agent import graph
    reload(graph)
    r = graph.run_event({"subject": "nginx down", "from": "m@x.com", "body": "服务宕机", "source": "test"})
    assert r["status"] == "escalated", r
    assert calls["verify"] == 2, f"应重试一次，实际 {calls['verify']}"
    print("✓ 重试后升级路径 OK")


if __name__ == "__main__":
    import app.db as db
    db.ensure_init()
    conn = db.get_conn()
    with conn.cursor() as cur:
        cur.execute("TRUNCATE TABLE ops_events")
    test_escalate_path()
    test_handled_path()
    test_retry_then_escalate()
    print("全部流程测试通过 ✅")
