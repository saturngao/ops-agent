"""自然语言数据分析：数据查询 / 趋势分析 / 异常识别 / 指标对比"""
import time

from .llm import chat, LLMError
from .storage import metrics_summary, list_events, list_metrics

SYSTEM = (
    "你是「运维 Agent」的数据分析师。基于给定的服务器指标数据（JSON）与最近运维事件，回答用户的自然语言问题。"
    "支持：数据查询、趋势分析、异常识别、指标对比。要求：\n"
    "1. 只依据给定数据回答，不要编造数据；数据不足时明确说明。\n"
    "2. 结论先行，给出关键数字；趋势/异常/对比类问题给出具体依据（时间点、数值、变化幅度）。\n"
    "3. 若发现异常，给出可能原因与建议动作。\n"
    "4. 用中文、Markdown 简洁排版回答，控制在 400 字内。"
)


def analyze(question: str, hours: float = 24) -> dict:
    summary = metrics_summary(hours=hours)
    rows = list_metrics(hours=hours, limit=300)
    # 压缩为时间序列样本（最多 60 个点）
    step = max(1, len(rows) // 60)
    series = [
        {
            "t": time.strftime("%m-%d %H:%M", time.localtime(r["ts"])),
            "cpu_pct": r.get("cpu_pct"),
            "mem_used_pct": r.get("mem_used_pct"),
            "disk_used_pct": r.get("disk_used_pct"),
            "load1": r.get("load1"),
        }
        for r in rows[::step]
    ][-60:]
    events = [
        {"time": e.get("created_at"), "subject": e.get("subject"),
         "type": (e.get("parsed") or {}).get("type"), "status": e.get("status"),
         "summary": e.get("summary", "")}
        for e in list_events(limit=20)
    ]

    user = (
        f"用户问题: {question}\n\n"
        f"统计摘要（近{hours}小时）: {summary}\n\n"
        f"时间序列样本（最多60点）: {series}\n\n"
        f"最近运维事件: {events}"
    )
    try:
        answer = chat([
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": user},
        ], temperature=0.3, max_tokens=1200, node="analysis")
        return {"ok": True, "answer": answer, "hours": hours,
                "data_points": len(series)}
    except LLMError as e:
        return {"ok": False, "answer": f"分析失败: {e}", "hours": hours,
                "data_points": len(series)}
