"""指标采集器：通过 SSH 采集服务器指标；未配置 SSH 时使用演示模拟数据"""
import logging
import random
import threading
import time

from . import ssh_client
from .config import load_config
from .storage import append_metric

log = logging.getLogger("ops-agent.collector")

_collector_thread: threading.Thread | None = None
stop_flag = threading.Event()
last_run: dict = {}


def _parse_linewidth_free(line: str) -> dict:
    # free -m: Mem: total used free shared buff/cache available
    parts = line.split()
    if len(parts) < 6 or not parts[0].startswith("Mem"):
        return {}
    total, used = int(parts[1]), int(parts[2])
    available = int(parts[6]) if len(parts) >= 7 else total - used
    return {"mem_total_mb": total, "mem_used_mb": used,
            "mem_used_pct": round(used / total * 100, 1) if total else 0}


def _collect_via_ssh() -> dict | None:
    r = ssh_client.run_command(
        "cat /proc/stat | grep '^cpu ' && free -m && df -h / && cat /proc/loadavg && uptime",
        timeout=20)
    if not r.get("ok"):
        return None
    out = r["stdout"]
    record: dict = {}
    try:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("cpu "):
                p = [int(x) for x in line.split()[1:8]]
                total = sum(p)
                idle = p[3] + p[4]
                record["cpu_pct"] = round((1 - idle / total) * 100, 1) if total else 0
            elif line.startswith("Mem:"):
                record.update(_parse_linewidth_free(line))
            elif "%" in line and "/" in line and record.get("disk_used_pct") is None and line.split()[0].startswith("/"):
                pass
        # df -h / 行：/dev/xxx size used avail use% mounted
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 6 and parts[-1] == "/" and parts[-2].endswith("%"):
                record["disk_used_pct"] = float(parts[-2].rstrip("%"))
                break
        # loadavg
        la = [x for x in out.splitlines() if "load average" in x]
        if la:
            record["load1"] = float(la[-1].split("load average:")[1].split(",")[0])
        elif record.get("load1") is None:
            pass
    except Exception as e:
        log.warning("指标解析失败: %s", e)
    if "cpu_pct" in record or "mem_used_pct" in record:
        record["source"] = "ssh"
        return record
    return None


def _collect_demo() -> dict:
    """演示模式：生成带日间波动与偶发毛刺的模拟指标。"""
    hour = time.localtime().tm_hour
    day_factor = 0.6 + 0.35 * max(0, __import__("math").sin((hour - 6) / 24 * 2 * 3.14159))
    spike = 1.0
    if random.random() < 0.04:
        spike = random.uniform(1.3, 1.8)  # 偶发负载毛刺
    cpu = min(99, round((18 + 55 * day_factor) * spike * random.uniform(0.9, 1.1), 1))
    mem = min(98, round((35 + 25 * day_factor) * random.uniform(0.97, 1.06), 1))
    disk = min(97, round(58 + random.uniform(-1, 2.5), 1))
    return {
        "cpu_pct": cpu,
        "mem_used_pct": mem,
        "disk_used_pct": disk,
        "load1": round(cpu / 100 * 4 * spike, 2),
        "source": "demo",
    }


def collect_once() -> dict | None:
    cfg = load_config()
    record = None
    if ssh_client.ssh_enabled():
        record = _collect_via_ssh()
    if record is None and cfg["collector"].get("demo_mode", True):
        record = _collect_demo()
    if record:
        append_metric(record)
    last_run.update({"ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                     "ok": record is not None,
                     "source": (record or {}).get("source", "none")})
    return record


def _loop():
    while not stop_flag.is_set():
        cfg = load_config()
        interval = int(cfg["collector"].get("interval", 300))
        try:
            if cfg["collector"].get("enabled", True):
                collect_once()
        except Exception as e:
            log.warning("采集异常: %s", e)
        stop_flag.wait(max(30, interval))


def start():
    global _collector_thread
    if _collector_thread and _collector_thread.is_alive():
        return
    stop_flag.clear()
    _collector_thread = threading.Thread(target=_loop, name="metrics-collector", daemon=True)
    _collector_thread.start()
    log.info("指标采集线程已启动")


def stop():
    stop_flag.set()
