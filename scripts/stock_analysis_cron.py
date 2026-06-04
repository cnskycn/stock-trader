#!/usr/bin/env python3
"""
000967 每日股票分析 - Cron 专用版
直接输出格式化的分析结果，不依赖 AI Agent
"""
import subprocess
import sys
import re
from datetime import datetime

SCRIPT = "/root/.openclaw/workspace/skills/stock-trader/scripts/stock_trader.py"
STOCK_CODE = "000967"

def run():
    try:
        result = subprocess.run(
            [sys.executable, SCRIPT, STOCK_CODE],
            capture_output=True,
            text=True,
            timeout=60
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        return "❌ 执行超时（60秒），请稍后重试"
    except Exception as e:
        return f"❌ 执行出错：{e}"

    # 解析关键数据
    source = re.search(r"【数据来源】(\S+)", output)
    price  = re.search(r"【当前股价】([\d.]+)元", output)
    action = re.search(r"【今日建议】(\S+)", output)
    exec_  = re.search(r"【操作执行】(\S+)", output)
    pos    = re.search(r"【当前持仓】(\S+)", output)
    cash   = re.search(r"【资金余额】([\d.]+)", output)
    pnl    = re.search(r"【持仓盈亏】([^\n]+)", output)

    # 提取理由
    reason_match = re.search(r"理由[:：]\s*(.+)", output)
    reason = reason_match.group(1).strip() if reason_match else ""

    date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # 构建 Feishu 消息
    lines = [
        f"📈 **000967 盈峰环境 — 每日分析**",
        f"🕐 {date_str}",
        "",
        f"**当前股价**：{price.group(1) if price else '?'} 元" if price else "",
        f"**数据来源**：{source.group(1) if source else '?'}",
        "",
        f"**今日建议**：{action.group(1) if action else '?'}",
        f"**操作执行**：{exec_.group(1) if exec_ else '?'}",
        "",
        f"**当前持仓**：{pos.group(1) if pos else '?'}",
        f"**资金余额**：{cash.group(1) if cash else '?'} 元",
    ]
    if pnl:
        lines.append(f"**持仓盈亏**：{pnl.group(1).strip()}")
    if reason:
        lines.extend(["", f"💡 *{reason}*"])

    return "\n".join(line for line in lines if line)

if __name__ == "__main__":
    print(run())
