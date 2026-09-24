"""本地文件配置存储：data/config.json"""
import json
import os
import threading
from typing import Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")

_lock = threading.Lock()

DEFAULT_CONFIG: dict[str, Any] = {
    "mysql": {
        # MySQL 连接为引导配置，保存在本地文件 data/config.json；其余配置均存入 MySQL
        "host": "127.0.0.1",
        "port": 3306,
        "user": "root",
        "password": "root",
        "database": "ops_agent",
    },
    "llm": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "api_key": "",
        "model": "qwen-plus",
        "temperature": 0.2,
    },
    "email": {
        # 邮件 MCP 服务配置：IMAP 实时接收通知邮件 + SMTP 发送升级邮件
        "enabled": False,
        "imap_host": "imap.qq.com",
        "imap_port": 993,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "username": "",          # 邮箱地址，如 xxxx@qq.com
        "auth_code": "",         # IMAP/SMTP 授权码（非登录密码）
        "poll_interval": 60,     # 秒
        "escalate_to": "596826873@qq.com",
        "subject_keywords": ["告警", "监控", "运维", "alert", "ops", "alarm"],
    },
    "ssh": {
        "enabled": False,
        "host": "",
        "port": 22,
        "username": "root",
        "password": "",
        "key_path": "",
        "allow_restart": True,   # 是否允许 Agent 执行 systemctl restart
        "timeout": 30,
    },
    "collector": {
        "enabled": True,
        "interval": 300,         # 秒，指标采集周期
        "demo_mode": True,       # 未配置 SSH 时用模拟数据，便于体验
    },
}


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def get_mysql_config() -> dict:
    """MySQL 连接参数只来自本地文件（引导配置）。"""
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
        except Exception:
            cfg = {}
    return _deep_merge(DEFAULT_CONFIG["mysql"], (cfg or {}).get("mysql", {}))


def _load_local() -> dict:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _load_db_config() -> dict | None:
    """从 MySQL ops_config 表读取配置；数据库不可用时返回 None。"""
    try:
        from . import db
        db.ensure_init()
        conn = db.get_conn()
        with conn.cursor() as cur:
            cur.execute("SELECT data FROM ops_config WHERE id=1")
            row = cur.fetchone()
        if row and row[0]:
            return json.loads(row[0])
    except Exception as e:
        import logging
        logging.getLogger("ops-agent.config").warning("从 MySQL 读取配置失败，使用本地文件: %s", e)
    return None


def load_config() -> dict:
    with _lock:
        local = _load_local()
        # mysql 段始终以本地文件为准（引导配置）
        db_cfg = _load_db_config() or {}
        merged = _deep_merge(DEFAULT_CONFIG, _deep_merge(db_cfg, {"mysql": local.get("mysql", {})}))
        return merged


def save_config(patch: dict) -> dict:
    with _lock:
        cfg = _deep_merge(DEFAULT_CONFIG, _deep_merge(_load_local(), _load_db_config() or {}))
        cfg = _deep_merge(cfg, patch or {})
        # 1) mysql 引导配置写本地文件，其余写 MySQL
        mysql_patch = (patch or {}).get("mysql")
        local = _load_local()
        if mysql_patch is not None:
            local["mysql"] = _deep_merge(local.get("mysql", {}), mysql_patch)
        else:
            local.setdefault("mysql", cfg["mysql"])
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(local, f, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)
        # 2) 全量配置存 MySQL ops_config
        try:
            from . import db
            db.ensure_init()
            conn = db.get_conn()
            with conn.cursor() as cur:
                cur.execute("REPLACE INTO ops_config (id, data) VALUES (1, %s)",
                            (json.dumps(cfg, ensure_ascii=False),))
        except Exception as e:
            import logging
            logging.getLogger("ops-agent.config").warning("配置写入 MySQL 失败: %s", e)
        return cfg


def redacted(cfg: dict) -> dict:
    """返回给前端时隐藏密钥明文。"""
    import copy
    c = copy.deepcopy(cfg)
    if c.get("llm", {}).get("api_key"):
        c["llm"]["api_key"] = "******"
    if c.get("email", {}).get("auth_code"):
        c["email"]["auth_code"] = "******"
    if c.get("ssh", {}).get("password"):
        c["ssh"]["password"] = "******"
    if c.get("mysql", {}).get("password"):
        c["mysql"]["password"] = "******"
    return c
