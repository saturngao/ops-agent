"""SSH 执行器：paramiko 连接服务器执行命令，带安全防护"""
import logging
import re

import paramiko

from .config import load_config

log = logging.getLogger("ops-agent.ssh")

# 危险命令黑名单（正则），Agent 永远不允许执行
DANGEROUS_PATTERNS = [
    r"rm\s+(-\w*\s+)*/(\s|$)", r"mkfs", r"shutdown", r"reboot\s+-f", r"halt",
    r"dd\s+.*of=/dev/", r">\s*/dev/sd[a-z]", r"chmod\s+-R\s+777\s+/",
    r"chown\s+-R.*\s+/\s*$", r":\(\)\{.*\};:", r"fork\(\)", r"curl.*\|\s*(ba)?sh",
    r"wget.*\|\s*(ba)?sh", r"iptables\s+-F", r"passwd", r"userdel", r"dropdb",
    r"truncate\s+table", r"drop\s+(table|database)", r">\s*/etc/passwd",
]

# 允许的诊断/处置命令白名单（前缀匹配）
ALLOWED_PREFIXES = [
    "df ", "free ", "top -bn1", "uptime", "cat /proc/loadavg", "cat /proc/meminfo",
    "ps ", "systemctl status", "journalctl ", "tail ", "head ", "grep ", "dmesg ",
    "netstat ", "ss ", "du ", "ls ", "who", "w ", "vmstat ", "iostat ", "sar ",
    "ping -c", "systemctl restart ", "systemctl start ", "systemctl stop ",
    "service ", "find /var/log", "lsblk", "hostname", "date", "cat /etc/os-release",
    "kill ", "nproc", "lscpu", "cat /proc/stat", "sync",
]


def is_command_allowed(cmd: str) -> tuple[bool, str]:
    c = cmd.strip()
    low = c.lower()
    for p in DANGEROUS_PATTERNS:
        if re.search(p, low):
            return False, f"命中危险命令规则: {p}"
    for prefix in ALLOWED_PREFIXES:
        if low.startswith(prefix):
            return True, ""
    return False, "不在命令白名单内"


def ssh_enabled() -> bool:
    cfg = load_config()["ssh"]
    return bool(cfg.get("enabled") and cfg.get("host"))


def run_command(cmd: str, timeout: int | None = None) -> dict:
    """在配置的服务器上执行一条命令，返回 {ok, stdout, stderr, exit_code}。"""
    cfg = load_config()["ssh"]
    if not ssh_enabled():
        return {"ok": False, "stdout": "", "stderr": "SSH 未启用或未配置服务器地址", "exit_code": -1}

    timeout = timeout or int(cfg.get("timeout", 30))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        kwargs: dict = {
            "hostname": cfg["host"],
            "port": int(cfg.get("port", 22)),
            "username": cfg.get("username", "root"),
            "timeout": 15,
        }
        if cfg.get("key_path"):
            kwargs["key_filename"] = cfg["key_path"]
        elif cfg.get("password"):
            kwargs["password"] = cfg["password"]
        client.connect(**kwargs)
        stdin, stdout, stderr = client.exec_command(cmd, timeout=timeout)
        out = stdout.read().decode("utf-8", "ignore")
        err = stderr.read().decode("utf-8", "ignore")
        code = stdout.channel.recv_exit_status()
        return {"ok": code == 0, "stdout": out[:8000], "stderr": err[:2000], "exit_code": code}
    except Exception as e:
        log.warning("SSH 执行失败: %s", e)
        return {"ok": False, "stdout": "", "stderr": f"SSH 连接/执行异常: {e}", "exit_code": -1}
    finally:
        try:
            client.close()
        except Exception:
            pass


def run_commands(cmds: list[str]) -> list[dict]:
    return [{"cmd": c, **run_command(c)} for c in cmds]


def test_connection() -> dict:
    if not ssh_enabled():
        return {"ok": False, "message": "SSH 未启用或未配置"}
    r = run_command("hostname && uptime")
    if r["ok"]:
        return {"ok": True, "message": "连接成功", "output": r["stdout"][:300]}
    return {"ok": False, "message": r["stderr"] or "连接失败"}
