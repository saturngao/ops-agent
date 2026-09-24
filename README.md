# 运维 Agent 🤖

一个基于 **Python + FastAPI + LangGraph** 的智能运维 Agent Web 应用。通过**邮件 MCP 服务**实时接收运维通知邮件，由**大模型**解析、分诊、规划命令并通过 **SSH** 在服务器上自动处置，处理结果自动入库；无法自动处理的事件自动发送**升级邮件**到指定邮箱。同时支持基于服务器指标数据的**自然语言数据分析**（查询 / 趋势 / 异常识别 / 指标对比）。

---

## ✨ 功能特性

| 模块 | 说明 |
|---|---|
| 📧 邮件 MCP 服务 | IMAP 轮询实时接收通知邮件（支持主题关键词过滤），SMTP 发送升级告警邮件 |
| 🤖 LangGraph Agent | 事件解析 → 分诊 → 命令规划 → SSH 执行 → 结果验证 → 记录/升级，最多 2 轮重试 |
| 🛡️ 安全防护 | 危险命令黑名单拦截（`rm -rf /`、`mkfs`、管道执行脚本等）+ 命令白名单校验 |
| 💬 自然语言分析 | 基于本地指标数据的查询、趋势分析、异常识别、指标对比 |
| 📊 可视化仪表盘 | CPU / 内存 / 磁盘 / 负载趋势图、事件列表与处理轨迹、一键注入模拟告警邮件 |
| 🗄️ MySQL 存储 | 事件、指标、配置、LLM 调用日志全部存入本地 MySQL，首次启动自动建库建表 |
| 🧪 演示模式 | 未配置 SSH 时自动生成模拟指标，无需真实服务器即可体验完整流程 |

---

## 🏗️ 架构与处理流程

```
告警邮件 (IMAP) ──┐
                  ├─▶ [邮件 MCP 服务] ──▶ LangGraph Agent ──▶ 处理结果 ──▶ MySQL
模拟邮件 (Web UI) ─┘                        │
                                            ├─ 解析 parse      (LLM 提取结构化事件)
                                            ├─ 分诊 triage     (LLM 判断能否自动处理)
                                            ├─ 规划 plan       (LLM 生成安全命令序列)
                                            ├─ 执行 execute    (paramiko SSH，危险命令拦截)
                                            ├─ 验证 verify     (LLM 判断是否解决，最多重试 2 轮)
                                            └─ 记录/升级
                                                ├─ 可处理   → 事件标记 handled，入库
                                                └─ 不可处理 → 发送升级邮件 → 事件标记 escalated，入库
```

**技术栈**：FastAPI · LangGraph · langchain-core · paramiko · PyMySQL · httpx · ECharts

---

## 📁 项目结构

```
ops-agent/
├── main.py                  # FastAPI 入口 + REST API + 后台线程（邮件轮询/指标采集）
├── requirements.txt
├── test_flow.py             # 离线流程测试（桩替 LLM/SSH，覆盖三条 Agent 链路）
├── app/
│   ├── db.py                # MySQL 层：连接管理、自动建库建表、旧文件迁移
│   ├── config.py            # 配置读写（MySQL 存储，本地文件作引导配置）
│   ├── storage.py           # 事件/指标数据访问层
│   ├── llm.py               # OpenAI 兼容大模型客户端（流式 + 调用日志入库）
│   ├── email_mcp.py         # 邮件 MCP 服务：IMAP 收信 + SMTP 升级邮件
│   ├── ssh_client.py        # paramiko SSH 执行器（黑名单/白名单安全防护）
│   ├── metrics_collector.py # 指标采集（SSH 真实采集 / 演示模式模拟）
│   ├── analysis.py          # 自然语言数据分析
│   └── agent/graph.py       # LangGraph 状态图（Agent 核心流程）
├── static/index.html        # Web UI（仪表盘/事件/分析/调用日志/配置）
└── data/config.json         # MySQL 引导配置（连接信息）
```

---

## 🚀 快速开始

### 1. 环境要求

- Python 3.10+
- 本地 MySQL（5.7+ / MariaDB 均可），应用会自动创建数据库 `ops_agent` 及 4 张表

### 2. 安装依赖

```bash
cd ops-agent
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. 配置 MySQL 连接（引导配置）

编辑 `data/config.json`（首次运行前手动创建，或直接使用默认值）：

```json
{
  "mysql": {
    "host": "127.0.0.1",
    "port": 3306,
    "user": "root",
    "password": "xxxxx",
    "database": "ops_agent"
  }
}
```

### 4. 启动服务

```bash
python main.py
# 或
uvicorn main:app --host 0.0.0.0 --port 8000
```

访问 http://127.0.0.1:8000 打开 Web 控制台。

### 5. 完成必要配置（「⚙️ 配置」页）

1. **大模型**（必填）：填写 OpenAI 兼容接口，例如
   - DashScope 通义千问：`https://dashscope.aliyuncs.com/compatible-mode/v1` + API Key，模型 `qwen-plus`
   - DeepSeek：`https://api.deepseek.com/v1`，模型 `deepseek-chat`
2. **邮件 MCP 服务**：填写邮箱账号 + IMAP/SMTP 授权码（QQ 邮箱需在设置中开启 IMAP/SMTP 服务并使用**授权码**而非登录密码）
3. **SSH 服务器**：填写主机/端口/用户名/密码（或私钥路径），可一键测试连接
4. **指标采集**：SSH 可用时采集真实指标；未配置 SSH 时开启「演示模式」生成模拟数据

> 全部配置保存在 MySQL `ops_config` 表中，可在 Web 页面随时修改。

---

## 🧭 使用指南

### Web 控制台

| 页面 | 功能 |
|---|---|
| 📊 仪表盘 | 指标趋势图、事件统计、最近事件 |
| 📋 事件记录 | 事件列表、完整处理轨迹、重新处理、**注入模拟告警邮件**、立即检查邮件 |
| 💬 智能分析 | 自然语言提问，如「最近 24 小时 CPU 有什么异常？」「对比今天和昨天的内存占用」 |
| 📜 调用日志 | 所有 LLM 调用明细（来源节点、模型、耗时、错误） |
| ⚙️ 配置 | 大模型 / MySQL / 邮件 / SSH / 采集配置 |

### 体验完整流程（无需真实邮箱/服务器）

1. 「配置」页确认大模型已填写
2. 「事件记录」页点击 **🧪 注入模拟告警邮件**
3. Agent 自动完成 解析→分诊→（SSH 未配置时直接升级）→ 记录，可在事件轨迹中查看全过程

---

## 🔌 API 一览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/status` | 运行状态（邮件/SSH/模型/事件统计） |
| GET/POST | `/api/config` | 读取 / 保存配置 |
| GET | `/api/events` | 事件列表（含处理轨迹） |
| POST | `/api/events/simulate` | 注入模拟告警邮件 |
| POST | `/api/events/{id}/rerun` | 重新提交 Agent 处理 |
| POST | `/api/email/check` | 立即拉取一次通知邮件 |
| GET | `/api/metrics?hours=24` | 指标时序数据 |
| POST | `/api/analysis` | 自然语言数据分析 `{question, hours}` |
| GET | `/api/llm-logs` | LLM 调用日志 |
| POST | `/api/ssh/test` / `/api/db/test` | SSH / MySQL 连接测试 |

---

## 🗄️ 数据库表

| 表 | 内容 |
|---|---|
| `ops_events` | 事件及完整处理结果（解析/分诊/规划/执行/轨迹/升级状态） |
| `ops_metrics` | 指标时序数据（CPU/内存/磁盘/负载） |
| `ops_config` | 应用配置（JSON 全量存储） |
| `ops_llm_calls` | LLM 调用日志（节点、模型、耗时、错误） |

---

## 🧪 测试

```bash
python test_flow.py
```

离线桩测试覆盖三条 Agent 链路：✅ 自动处理（含危险命令拦截）· ✅ 验证失败重试后升级 · ✅ 直接转人工升级

---

## ⚠️ 注意事项

- **授权码 vs 密码**：QQ 邮箱的 IMAP/SMTP 必须使用授权码（设置 → 账户 → 开启服务 → 生成授权码）
- **Agent 命令安全**：默认禁止 `rm`、数据删除、配置修改等破坏性操作；重启服务需在配置中显式开启「允许重启」
- **发布上线**：云端沙箱无法访问本地 MySQL，如需发布到公网，请使用云端可达的 MySQL 或改回文件存储
- MySQL 不可用时，配置读取会自动降级到本地文件，但事件/指标写入会失败，请保持 MySQL 服务运行

---

## 📄 License

MIT
