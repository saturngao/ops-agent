"""LangGraph 运维 Agent：邮件事件 → 解析 → 分诊 → 规划 → SSH 执行 → 验证 → 记录/升级"""
import logging
import time

from langgraph.graph import StateGraph, END
from typing_extensions import TypedDict

from .. import ssh_client, email_mcp
from ..config import load_config
from ..llm import chat_json, LLMError
from ..storage import save_event, update_event, metrics_summary

log = logging.getLogger("ops-agent.graph")

MAX_ATTEMPTS = 2


class AgentState(TypedDict, total=False):
    event: dict            # 原始事件（含 subject/from/body/source）
    parsed: dict           # LLM 解析后的结构化事件
    triage: dict           # 分诊结论
    plan: dict             # 命令规划
    results: list          # 执行结果
    verify: dict           # 验证结论
    status: str            # parsing/triage/planning/executing/handled/escalated/failed
    attempts: int
    trace: list            # 处理轨迹
    error: str


def _trace(state: dict, node: str, note: str) -> list:
    t = list(state.get("trace") or [])
    t.append({"node": node, "time": time.strftime("%H:%M:%S"), "note": note[:300]})
    return t


# ---------------- Nodes ----------------

def parse_event(state: AgentState) -> AgentState:
    ev = state["event"]
    try:
        parsed = chat_json([
            {"role": "system", "content": (
                "你是运维事件解析器。从告警邮件中提取结构化信息，只返回 JSON："
                '{"type":"事件类型(如 磁盘告警/服务宕机/CPU过载/内存告警/日志错误/其他)",'
                '"severity":"critical|high|medium|low",'
                '"service":"涉及的服务或主机，未知填 unknown",'
                '"summary":"一句话摘要",'
                '"is_ops_event": true|false}。'
                "若邮件内容与运维/服务器/监控完全无关，则 is_ops_event=false。")}
        ] + [{"role": "user", "content": f"主题: {ev.get('subject','')}\n发件人: {ev.get('from','')}\n正文:\n{ev.get('body','')[:3000]}"}], node="parse")
    except LLMError as e:
        return {"status": "failed", "error": f"事件解析失败: {e}",
                "trace": _trace(state, "parse", f"解析失败: {e}")}
    if not parsed.get("is_ops_event", True):
        return {"parsed": parsed, "status": "ignored",
                "trace": _trace(state, "parse", "判定为非运维事件，忽略")}
    return {"parsed": parsed, "trace": _trace(state, "parse", f"解析完成: {parsed.get('type','?')} / {parsed.get('severity','?')}")}


def triage(state: AgentState) -> AgentState:
    parsed = state.get("parsed", {})
    ev = state["event"]
    try:
        tri = chat_json([
            {"role": "system", "content": (
                "你是运维分诊器。判断该事件能否由自动化 Agent 通过 SSH 执行安全的诊断/重启/清理类命令处理。"
                "只返回 JSON："
                '{"auto_handle": true|false, "reason":"判断理由（中文，50字内）"}。'
                "可自动处理的例子：磁盘清理、服务重启、日志排查、进程异常。"
                "必须人工的例子：硬件故障、安全入侵、数据删除、数据库损坏、需求不明确、涉及资金或数据不可逆操作。")}
        ] + [{"role": "user", "content": f"事件: {parsed}\n邮件正文节选: {ev.get('body','')[:1000]}"}], node="triage")
    except LLMError as e:
        tri = {"auto_handle": False, "reason": f"分诊模型调用失败，保守转人工: {e}"}
    tri.setdefault("auto_handle", False)
    return {"triage": tri, "trace": _trace(state, "triage", f"分诊: {'自动处理' if tri['auto_handle'] else '转人工'} - {tri.get('reason','')}")}


def plan_commands(state: AgentState) -> AgentState:
    cfg = load_config()
    allow_restart = cfg["ssh"].get("allow_restart", True)
    parsed = state.get("parsed", {})
    prev_results = state.get("results") or []
    try:
        plan = chat_json([
            {"role": "system", "content": (
                "你是 Linux 运维命令规划器。为该事件规划最多 5 条 SSH 命令（当前为第 "
                f"{state.get('attempts', 1)} 轮）。"
                + ("允许使用 systemctl restart/start/stop 重启服务。" if allow_restart else "禁止重启服务。")
                + "只能使用常见只读诊断命令（df/free/ps/journalctl/tail/grep/du/ss/uptime/top -bn1 等），"
                  "处置类仅限 systemctl restart/start/stop 与 kill。严禁 rm、删除数据、修改配置文件、任何破坏性操作。"
                "只返回 JSON："
                '{"diagnosis":"初步判断",'
                '"commands":[{"cmd":"命令","purpose":"目的"}]}。'
                "若无需执行任何命令，commands 返回空数组。")}
        ] + [{"role": "user", "content": (
            f"事件: {parsed}\n"
            f"服务器指标摘要: {metrics_summary(hours=6)}\n"
            + (f"上一轮执行结果（请据此调整命令）: {prev_results}" if prev_results else "")
        )}], node="plan")
    except LLMError as e:
        return {"plan": {}, "status": "failed", "error": f"命令规划失败: {e}",
                "trace": _trace(state, "plan", f"规划失败: {e}")}
    cmds = [c.get("cmd", "") for c in plan.get("commands", []) if c.get("cmd")]
    return {"plan": plan, "trace": _trace(state, "plan", f"规划 {len(cmds)} 条命令: {plan.get('diagnosis','')[:120]}")}


def execute(state: AgentState) -> AgentState:
    plan = state.get("plan") or {}
    cmds = [c.get("cmd", "").strip() for c in plan.get("commands", []) if c.get("cmd", "").strip()]
    if not cmds:
        return {"results": state.get("results") or [],
                "trace": _trace(state, "execute", "本轮无命令需要执行")}
    if not ssh_client.ssh_enabled():
        return {"results": [], "error": "SSH 未启用/未配置服务器，无法自动执行命令",
                "trace": _trace(state, "execute", "SSH 未配置，跳过执行")}
    results = list(state.get("results") or [])
    for cmd in cmds:
        allowed, why = ssh_client.is_command_allowed(cmd)
        if not allowed:
            results.append({"cmd": cmd, "ok": False, "stdout": "", "stderr": f"已拦截: {why}", "exit_code": -1})
            continue
        r = ssh_client.run_command(cmd)
        results.append({"cmd": cmd, **r})
    ok_n = sum(1 for r in results if r.get("ok"))
    return {"results": results,
            "trace": _trace(state, "execute", f"执行 {len(cmds)} 条命令，成功 {ok_n} 条")}


def verify(state: AgentState) -> AgentState:
    parsed = state.get("parsed", {})
    results = state.get("results") or []
    if not ssh_client.ssh_enabled() and not results:
        return {"verify": {"success": False, "summary": "未配置 SSH，无法执行自动处置"},
                "trace": _trace(state, "verify", "无执行环境，验证不通过")}
    try:
        v = chat_json([
            {"role": "system", "content": (
                "你是运维验证器。根据事件与命令执行结果，判断问题是否已被识别/解决。"
                "只返回 JSON："
                '{"success": true|false, "resolved": true|false,'
                '"summary":"处理结论（中文，100字内）",'
                '"need_more": true|false}  '
                "need_more 表示还需要再执行一轮命令（最多再来一轮）。")}
        ] + [{"role": "user", "content": f"事件: {parsed}\n执行结果: {results}"}], node="verify")
    except LLMError as e:
        v = {"success": False, "resolved": False, "need_more": False,
             "summary": f"验证模型调用失败: {e}"}
    return {"verify": v, "trace": _trace(state, "verify", v.get("summary", ""))}


def record_event(state: AgentState) -> AgentState:
    ev = dict(state["event"])
    verify = state.get("verify") or {}
    ev.update({
        "parsed": state.get("parsed"),
        "triage": state.get("triage"),
        "plan": state.get("plan"),
        "results": state.get("results") or [],
        "trace": state.get("trace") or [],
        "status": "handled",
        "summary": verify.get("summary", ""),
        "resolved": verify.get("resolved", False),
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    saved = save_event(ev)
    log.info("事件 %s 已处理并记录", saved["id"])
    return {"status": "handled"}


def escalate(state: AgentState) -> AgentState:
    ev = dict(state["event"])
    parsed = state.get("parsed") or {}
    triage = state.get("triage") or {}
    ev.update({
        "parsed": parsed,
        "triage": triage,
        "plan": state.get("plan"),
        "results": state.get("results") or [],
        "trace": state.get("trace") or [],
        "status": "escalated",
        "summary": (state.get("verify") or {}).get("summary", "") or triage.get("reason", ""),
        "escalation": None,
        "finished_at": time.strftime("%Y-%m-%d %H:%M:%S"),
    })
    saved = save_event(ev)
    # 发送升级邮件到指定邮箱（发送失败不影响事件记录）
    try:
        mail_result = email_mcp.send_escalation(saved)
    except Exception as e:
        log.warning("升级邮件异常: %s", e)
        mail_result = {"ok": False, "message": f"升级邮件发送异常: {e}"}
    update_event(saved["id"], {"escalation": mail_result})
    log.info("事件 %s 已升级: %s", saved["id"], mail_result.get("message"))
    return {"status": "escalated"}


# ---------------- 路由 ----------------

def route_after_parse(state: AgentState) -> str:
    if state.get("status") in ("failed", "ignored"):
        return END if state.get("status") == "ignored" else "escalate"
    return "triage"


def route_after_triage(state: AgentState) -> str:
    if (state.get("triage") or {}).get("auto_handle"):
        return "plan"
    return "escalate"


def route_after_verify(state: AgentState) -> str:
    v = state.get("verify") or {}
    if v.get("success"):
        return "record"
    if v.get("need_more") and state.get("attempts", 1) < MAX_ATTEMPTS:
        return "plan"
    return "escalate"


def build_graph():
    g = StateGraph(AgentState)
    g.add_node("parse", parse_event)
    g.add_node("triage", triage)
    g.add_node("plan", plan_commands)
    g.add_node("execute", execute)
    g.add_node("verify", verify)
    g.add_node("record", record_event)
    g.add_node("escalate", escalate)
    g.set_entry_point("parse")
    g.add_conditional_edges("parse", route_after_parse, {"triage": "triage", "escalate": "escalate", END: END})
    g.add_conditional_edges("triage", route_after_triage, {"plan": "plan", "escalate": "escalate"})
    g.add_edge("plan", "execute")
    g.add_edge("execute", "verify")
    g.add_conditional_edges("verify", route_after_verify, {"plan": "plan", "record": "record", "escalate": "escalate"})
    g.add_edge("record", END)
    g.add_edge("escalate", END)
    return g.compile()


compiled_graph = build_graph()


def run_event(event: dict) -> dict:
    """运行一次完整的运维事件处理流程（同步）。"""
    state = {"event": event, "attempts": 1, "results": [], "trace": []}
    try:
        final = compiled_graph.invoke(state, {"recursion_limit": 25})
    except Exception as e:
        log.exception("Agent 运行异常")
        ev = dict(event)
        ev.update({"status": "escalated", "error": str(e),
                   "trace": _trace(state, "error", f"Agent 运行异常: {e}"),
                   "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        saved = save_event(ev)
        mail_result = email_mcp.send_escalation(saved)
        update_event(saved["id"], {"escalation": mail_result})
        return saved
    # 持久化最终状态（ignored/failed 也要落库）
    status = final.get("status", "failed")
    if status not in ("handled", "escalated"):
        ev = dict(event)
        ev.update({"parsed": final.get("parsed"), "trace": final.get("trace") or [],
                   "status": status, "error": final.get("error", ""),
                   "finished_at": time.strftime("%Y-%m-%d %H:%M:%S")})
        save_event(ev)
    return {"status": status}
