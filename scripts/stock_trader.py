#!/usr/bin/env python3
"""
模拟操盘手 - 核心脚本
支持多数据源：同花顺、新浪财经、腾讯财经

版本: v2.5 (2026-06-10)
更新:
  - 融入趋势交易逻辑（《传统价投为什么注定无缘AI行情》）
  - 硬止损：跌破MA20必走、持仓回撤15%必走、成本亏10%必走
  - 新增冷却期：止损出场后至少等3天才可重新买入
  - 收紧超跌买入：必须站稳MA20，防止空头趋势接飞刀
  - 优先右侧放量突破买入（MA5+MA10），超跌买入降为次选
  - 均线空头排列时绝对禁止买入
  - 新增RSI(14)辅助指标
  - 新增回测能力：可对历史数据验证策略表现
  - 新增绩效指标：CAGR、最大回撤、夏普比率、胜率
"""

import os
import sys
import json
import datetime
import requests
import math
import argparse
import csv
from pathlib import Path
from statistics import mean, stdev

# 尝试导入thsdk
THSDK_AVAILABLE = False
try:
    sys.path.insert(0, '/root/.local/lib/python3.12/site-packages')
    from thsdk import THS
    THSDK_AVAILABLE = True
except ImportError:
    pass

# 配置
STOCK_STATE_FILE = os.path.expanduser("~/.openclaw/workspace/memory/stock_position.json")
STOCK_FILE = os.path.expanduser("~/.openclaw/workspace/memory/stock_simulation.md")
DEFAULT_STOCK_CODE = "000967"
DEFAULT_INITIAL_CAPITAL = 100000


def get_data_from_ths(stock_code):
    """数据源1: 同花顺"""
    if not THSDK_AVAILABLE:
        return None, "同花顺未安装"
    try:
        with THS() as ths:
            result = ths.search_symbols(stock_code)
            ths_code = None
            name = ""
            if result and hasattr(result, 'data') and result.data:
                for item in result.data:
                    if item.get('MarketDisplay') in ['沪A', '深A']:
                        ths_code = item.get('THSCODE')
                        name = item.get('Name', '')
                        break
            if not ths_code:
                return None, "未找到股票"
            klines_result = ths.klines(ths_code, interval="day", count=60)
            if not klines_result or not hasattr(klines_result, 'data') or not klines_result.data:
                return None, "无K线数据"
            data = []
            for item in klines_result.data:
                time_val = item.get('时间')
                if hasattr(time_val, 'strftime'):
                    date_str = time_val.strftime('%Y-%m-%d')
                else:
                    date_str = str(time_val)[:10]
                data.append({
                    'date': date_str,
                    'open': float(item.get('开盘价', 0)),
                    'close': float(item.get('收盘价', 0)),
                    'high': float(item.get('最高价', 0)),
                    'low': float(item.get('最低价', 0)),
                    'volume': int(item.get('成交量', 0))
                })
            if not data:
                return None, "K线数据解析失败"
            latest = data[-1]
            return {
                'source': '同花顺',
                'name': name,
                'current_price': latest.get('close', 0),
                'klines': data
            }, None
    except Exception as e:
        return None, f"同花顺错误: {str(e)}"


def get_data_from_sina(stock_code):
    """数据源2: 新浪财经"""
    try:
        code = 'sz' + stock_code if len(stock_code) == 6 else stock_code
        url = f'https://hq.sinajs.cn/list={code}'
        headers = {'Referer': 'https://finance.sina.com.cn'}
        resp = requests.get(url, headers=headers, timeout=10)
        fields = resp.text.split('=')[1].strip('"').split(',')
        current_price = float(fields[3])
        name = fields[0]
        url2 = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen=60'
        klines_raw = json.loads(requests.get(url2, timeout=10).text)
        kline_data = []
        for k in klines_raw:
            kline_data.append({
                'date': k['day'][:10],
                'open': float(k['open']),
                'close': float(k['close']),
                'high': float(k['high']),
                'low': float(k['low']),
                'volume': int(k['volume'])
            })
        return {
            'source': '新浪财经',
            'name': name,
            'current_price': current_price,
            'klines': kline_data
        }, None
    except Exception as e:
        return None, f"新浪错误: {str(e)}"


def get_data_from_tencent(stock_code):
    """数据源3: 腾讯财经"""
    try:
        code = 'sz' + stock_code if len(stock_code) == 6 else stock_code
        url = f'https://qt.gtimg.cn/q={code}'
        resp = requests.get(url, timeout=10)
        fields = resp.text.split('~')
        current_price = float(fields[3])
        name = fields[1]
        return {
            'source': '腾讯财经',
            'name': name,
            'current_price': current_price,
            'klines': []
        }, None
    except Exception as e:
        return None, f"腾讯错误: {str(e)}"


def get_stock_data(stock_code):
    """多数据源获取股票数据"""
    result = get_data_from_ths(stock_code)
    if result[0]:
        return result
    result = get_data_from_sina(stock_code)
    if result[0]:
        return result
    result = get_data_from_tencent(stock_code)
    if result[0]:
        return result
    return None, "所有数据源均失败"


# ============================================================
# 技术指标计算
# ============================================================

def calc_ma(klines, period):
    if len(klines) < period:
        return None
    return sum(k['close'] for k in klines[-period:]) / period


def calc_rsi(klines, period=14):
    """计算RSI(14)，标准Wilder平滑法"""
    if len(klines) < period + 1:
        return None
    closes = [k['close'] for k in klines]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        return 100.0
    
    rsi_vals = [None] * (period + 1)
    rsi_vals[period] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))
    
    for i in range(period + 1, len(closes)):
        avg_gain = ((period - 1) * avg_gain + gains[i-1]) / period
        avg_loss = ((period - 1) * avg_loss + losses[i-1]) / period
        if avg_loss == 0:
            rsi_vals.append(100.0)
        else:
            rsi_vals.append(100.0 - (100.0 / (1.0 + avg_gain / avg_loss)))
    return rsi_vals[-1]


def calc_rsi_series(klines, period=14):
    """计算完整RSI序列（用于回测）"""
    if len(klines) < period + 1:
        return [None] * len(klines)
    closes = [k['close'] for k in klines]
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    result = [None] * (period + 1)
    result[period] = 100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0
    
    for i in range(period + 1, len(closes)):
        avg_gain = ((period - 1) * avg_gain + gains[i-1]) / period
        avg_loss = ((period - 1) * avg_loss + losses[i-1]) / period
        result.append(100.0 - (100.0 / (1.0 + avg_gain / avg_loss)) if avg_loss > 0 else 100.0)
    return result


def calc_all_indicators(data, position=0, state=None):
    """计算所有技术指标"""
    current_price = data['current_price']
    klines = data.get('klines', [])
    if len(klines) < 5:
        return None

    ma5  = calc_ma(klines, 5)
    ma10 = calc_ma(klines, 10)
    ma20 = calc_ma(klines, 20)
    ma60 = calc_ma(klines, 60)

    above_ma5  = bool(ma5  and current_price > ma5)
    above_ma10 = bool(ma10 and current_price > ma10)
    above_ma20 = bool(ma20 and current_price > ma20)

    # MA5方向向上（近期趋势修复信号）
    ma5_up = bool(ma5 and len(klines) >= 6 and calc_ma(klines[:-1], 5) < ma5)

    # 均线空头排列（下跌趋势，禁止逆势买入）
    ma_bearish = bool(ma5 and ma10 and ma20 and ma5 < ma10 < ma20)

    # RSI(14)
    rsi14 = calc_rsi(klines, 14)

    # RSI解读
    if rsi14 is not None:
        if rsi14 > 70:
            rsi_signal = "RSI超买⚠️"
        elif rsi14 < 30:
            rsi_signal = "RSI超卖📈"
        elif rsi14 > 55:
            rsi_signal = "RSI偏强"
        elif rsi14 < 45:
            rsi_signal = "RSI偏弱"
        else:
            rsi_signal = "RSI中性"
    else:
        rsi_signal = "RSI不足"

    # 20日最高/最低
    high_20 = max(k['high'] for k in klines[-20:]) if len(klines) >= 20 else max(k['high'] for k in klines)
    drop_from_high = (high_20 - current_price) / high_20 * 100

    # 持仓回撤跟踪
    cost_basis = state.get('cost_basis', 0) if state else 0
    max_price  = state.get('max_price', 0)  if state else 0
    if position > 0 and max_price > 0:
        drawdown_from_max = (max_price - current_price) / max_price * 100
        total_gain = (current_price - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0
    else:
        drawdown_from_max = 0.0
        total_gain = 0.0

    # 成交量分析
    recent_vols = [k['volume'] for k in klines[-5:]]
    avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 1
    vol_ratio = recent_vols[-1] / avg_vol if avg_vol else 0
    vol_shrinking = bool(vol_ratio < 0.7)

    # 近5日涨跌
    recent_changes = []
    for i in range(1, min(6, len(klines))):
        chg = (klines[-i]['close'] - klines[-i-1]['close']) / klines[-i-1]['close'] * 100
        recent_changes.append(chg)

    consecutive_down = 0
    for chg in reversed(recent_changes[1:]):
        if chg < -0.5:
            consecutive_down += 1
        else:
            break

    narrowing_drop = bool(len(recent_changes) >= 2 and abs(recent_changes[0]) < abs(recent_changes[1]))
    star_candle = False
    if klines:
        last = klines[-1]
        body = abs(last['close'] - last['open'])
        range_ = last['high'] - last['low']
        if range_ > 0 and body < range_ * 0.3:
            star_candle = True

    return {
        'current_price': current_price,
        'ma5': ma5, 'ma10': ma10, 'ma20': ma20, 'ma60': ma60,
        'above_ma5': above_ma5, 'above_ma10': above_ma10, 'above_ma20': above_ma20,
        'ma5_up': ma5_up,
        'ma_bearish': ma_bearish,
        'high_20': high_20,
        'drop_from_high': drop_from_high,
        'drawdown_from_max': drawdown_from_max,
        'total_gain': total_gain,
        'rsi14': rsi14,
        'rsi_signal': rsi_signal,
        'vol_ratio': vol_ratio,
        'consecutive_down': consecutive_down,
        'vol_shrinking': vol_shrinking,
        'narrowing_drop': narrowing_drop,
        'star_candle': star_candle,
        'recent_changes': recent_changes,
        'klines': klines,
    }


def format_indicators(ind):
    lines = []
    lines.append(f"股价：{ind['current_price']:.3f}元")
    if ind['ma5']:
        lines.append(f"MA5：{ind['ma5']:.3f} | MA10：{ind['ma10']:.3f} | MA20：{ind['ma20']:.3f}" + (f" | MA60：{ind['ma60']:.3f}" if ind['ma60'] else ""))
    lines.append(f"20日最高：{ind['high_20']:.3f}，从高点回撤：{ind['drop_from_high']:.1f}%")
    lines.append(f"均线多头：↑5={ind['above_ma5']} ↑10={ind['above_ma10']} ↑20={ind['above_ma20']}")
    lines.append(f"MA5方向向上：{ind['ma5_up']}")
    lines.append(f"MA空头排列：{'⚠️是' if ind['ma_bearish'] else '否'}")
    lines.append(f"RSI(14)：{ind['rsi14']:.1f} | {ind['rsi_signal']}")
    lines.append(f"成交量比：{ind['vol_ratio']:.2f}倍均量")
    lines.append(f"连续下跌：{ind['consecutive_down']}天")
    return '\n'.join(lines)


# ============================================================
# 趋势交易决策引擎 v2.5
# ============================================================

def trend_decide(data, position=0, state=None):
    """
    趋势交易决策 v2.5
    """
    ind = calc_all_indicators(data, position, state)
    if not ind:
        return "观望", "数据不足，无法分析", None

    current_price = ind['current_price']
    ma5  = ind['ma5']
    ma10 = ind['ma10']
    ma20 = ind['ma20']

    today = datetime.datetime.now().strftime('%Y-%m-%d')
    cooldown_until = state.get('cooldown_until', '') if state else ''
    in_cooldown = bool(cooldown_until and today < cooldown_until)

    # ---------- 持有中：止损检查 ----------
    if position > 0:
        cost_basis = state.get('cost_basis', 0)
        max_price  = state.get('max_price', 0)

        # 🔴 硬规则1：跌破MA20 → 必须止损
        if ma20 and current_price < ma20:
            return "卖出", (
                f"🔴【趋势破位止损】价格{current_price:.3f}<MA20({ma20:.3f})！"
                f"趋势已坏，不幻想反弹。盈亏{ind['total_gain']:+.1f}%，必须走！"
            ), ind

        # 🔴 硬规则2：从持仓最高回撤15% → 止损
        if max_price > 0 and ind['drawdown_from_max'] > 15:
            return "卖出", (
                f"🔴【回撤止损】从持仓最高{max_price:.3f}回撤{ind['drawdown_from_max']:.1f}%！"
                f"截断亏损！"
            ), ind

        # 🔴 硬规则3：从成本亏10% → 止损
        if cost_basis > 0 and ind['total_gain'] < -10:
            return "卖出", (
                f"🔴【成本止损】从成本{cost_basis:.3f}亏损{abs(ind['total_gain']):.1f}%！"
                f"超10%红线，认赔出局！"
            ), ind

        # 🟡 RSI辅助警示
        rsi_warn = ""
        if ind['rsi14'] and ind['rsi14'] > 80:
            rsi_warn = " | RSI极度超买⚠️"
        elif ind['rsi14'] and ind['rsi14'] < 25:
            rsi_warn = " | RSI严重超卖⚠️"

        alerts = []
        if ind['consecutive_down'] >= 3:
            alerts.append(f"连续下跌{ind['consecutive_down']}天")
        if ind['ma_bearish']:
            alerts.append("均线空头排列⚠️")
        if ma5 and not ind['above_ma5']:
            alerts.append("跌破MA5，短期走弱")
        if alerts:
            extra = rsi_warn + "".join([f" | {a}" for a in alerts])
            return "观望", f"持有中。{extra}。盈亏{ind['total_gain']:+.1f}%，趋势未破，继续持有。", ind
        return "观望", f"持有中。趋势完好，盈亏{ind['total_gain']:+.1f}%。", ind

    # ---------- 空仓：买入检查 ----------

    if in_cooldown:
        return "观望", (
            f"❌【冷却期】上次止损于{state.get('last_sell_date','')}，"
            f"冷却至{cooldown_until}，禁止追单！"
        ), ind

    if ind['ma_bearish']:
        return "观望", (
            "❌【趋势禁止】均线空头排列（MA5<MA10<MA20），趋势向下，禁止逆势买入！"
        ), ind

    if not ind['above_ma5']:
        return "观望", (
            f"🟡【观望】价格{current_price:.3f}<MA5({ma5:.3f})，趋势未修复，耐心等待。"
        ), ind

    # 🟢 买入条件A：右侧放量突破（首选）
    if ind['vol_ratio'] > 1.5 and ind['above_ma5'] and ind['above_ma10']:
        rsi_hint = f" | RSI{ind['rsi14']:.0f}" if ind['rsi14'] else ""
        return "买入", (
            f"🟢【右侧突破买入】放量{ind['vol_ratio']:.1f}倍！"
            f"价格站上MA5({ma5:.3f})/MA10({ma10:.3f})，趋势启动{rsi_hint}。"
            f"止损：当日最低下方！"
        ), ind

    # 🟢 买入条件B：超跌反弹（次选，需站稳MA20+MA5方向向上）
    if ind['drop_from_high'] >= 25 and ind['above_ma20'] and ind['ma5_up']:
        buy_signals = []
        if ind['vol_shrinking']:
            buy_signals.append("量缩")
        if ind['narrowing_drop']:
            buy_signals.append("跌幅收窄")
        if ind['star_candle']:
            buy_signals.append("十字星")
        if ind['rsi14'] and ind['rsi14'] < 40:
            buy_signals.append(f"RSI{ind['rsi14']:.0f}超卖")
        sig_text = '+'.join(buy_signals) if buy_signals else "站稳MA20+MA5向上"
        return "买入", (
            f"🟢【超跌反弹买入】从{ind['high_20']:.3f}回撤{ind['drop_from_high']:.1f}%，"
            f"{sig_text}。注：仅博反弹，快进快出！"
        ), ind

    return "观望", (
        f"🟡【观望】回撤{ind['drop_from_high']:.1f}%，"
        f"量比{ind['vol_ratio']:.2f}倍，RSI{ind['rsi14']:.0f if ind['rsi14'] else 'N/A'}，等待右侧信号。"
    ), ind


# ============================================================
# 策略回测模块（来自 stock-strategy-backtester 启发）
# ============================================================

def _max_drawdown(equity_curve):
    peak = equity_curve[0]
    worst = 0.0
    for x in equity_curve:
        if x > peak:
            peak = x
        dd = (x / peak) - 1.0 if peak > 0 else 0.0
        if dd < worst:
            worst = dd
    return abs(worst)


def _sharpe_ratio(daily_returns, risk_free_rate=0.03):
    if len(daily_returns) < 2:
        return 0.0
    vol = stdev(daily_returns)
    if vol == 0:
        return 0.0
    excess = mean(daily_returns) - risk_free_rate / 252
    return (excess / vol) * math.sqrt(252)


def _cagr(initial, final, days):
    if initial <= 0 or final <= 0 or days <= 0:
        return -1.0
    years = days / 365.25
    return (final / initial) ** (1.0 / years) - 1.0


def backtest_v25_rules(klines, initial_capital=100000, commission_bps=5, slippage_bps=2):
    """
    对历史K线数据回测v2.5规则的表现
    
    核心指标：
    - 总收益率、CAGR、最大回撤、夏普比率
    - 胜率、盈亏比、平均持仓天数
    - 交易次数、持仓时间占比
    """
    if len(klines) < 30:
        return {"error": "数据不足（需要至少30根K线）"}
    
    commission = commission_bps / 10000.0
    slippage = slippage_bps / 10000.0
    
    cash = initial_capital
    shares = 0.0
    in_position = False
    entry_date = None
    entry_price = 0.0
    entry_basis = 0.0
    trades = []
    equity_curve = [cash]
    days_in_market = 0
    
    for i in range(len(klines) - 1):
        closes = [k['close'] for k in klines]
        
        # 计算当日指标
        ma5  = sum(closes[max(0,i-4):i+1]) / min(5, i+1) if i >= 0 else None
        ma10 = sum(closes[max(0,i-9):i+1]) / min(10, i+1) if i >= 0 else None
        ma20 = sum(closes[max(0,i-19):i+1]) / min(20, i+1) if i >= 0 else None
        
        # 前日MA5方向
        ma5_yesterday = None
        if i >= 5:
            ma5_yesterday = sum(closes[i-4:i]) / 5
        
        ma5_up = (ma5_yesterday is not None and ma5 is not None and ma5 > ma5_yesterday)
        
        high_20 = max(k['high'] for k in klines[max(0,i-19):i+1])
        drop_from_high = (high_20 - closes[i]) / high_20 * 100
        
        recent_vols = [k['volume'] for k in klines[max(0,i-4):i+1]]
        avg_vol = sum(recent_vols) / len(recent_vols) if recent_vols else 1
        vol_ratio = recent_vols[-1] / avg_vol if avg_vol else 0
        
        ma_bearish = (ma5 and ma10 and ma20 and ma5 < ma10 < ma20)
        above_ma5 = bool(ma5 and closes[i] > ma5)
        above_ma10 = bool(ma10 and closes[i] > ma10)
        above_ma20 = bool(ma20 and closes[i] > ma20)
        
        # 决策（不使用前瞻数据）
        if not in_position:
            # 空仓：检查买入条件
            buy = False
            if not ma_bearish and above_ma5 and above_ma10 and vol_ratio > 1.5:
                buy = True  # 右侧突破
            elif (drop_from_high >= 25 and above_ma20 and ma5_up and 
                  closes[i] > ma20 and closes[i] > closes[i-1]):
                buy = True  # 超跌反弹
            
            if buy:
                px = klines[i+1]['open'] * (1.0 + slippage)
                cost = px * (1.0 + commission)
                if cost > 0:
                    shares = cash / cost
                    fee = shares * px * commission
                    entry_basis = shares * px + fee
                    cash -= entry_basis
                    in_position = True
                    entry_date = klines[i+1]['date']
                    entry_price = px
                    entry_max = px
        else:
            # 持仓最高价跟踪
            if closes[i] > entry_max:
                entry_max = closes[i]
            drawdown_max = (entry_max - closes[i]) / entry_max * 100
            
            # 止损检查
            sell = False
            reason = ""
            if ma20 and closes[i] < ma20:
                sell, reason = True, "跌破MA20"
            elif drawdown_max > 15:
                sell, reason = True, f"回撤{drawdown_max:.1f}%"
            
            if sell:
                px = klines[i+1]['open'] * (1.0 - slippage)
                gross = shares * px
                fee = gross * commission
                proceeds = gross - fee
                cash += proceeds
                ret = (proceeds / entry_basis - 1.0) * 100 if entry_basis > 0 else 0.0
                trades.append({
                    'entry': entry_date, 'exit': klines[i+1]['date'],
                    'entry_px': entry_price, 'exit_px': px,
                    'return_pct': ret, 'reason': reason,
                    'holding_days': max((datetime.datetime.strptime(klines[i+1]['date'], '%Y-%m-%d') - 
                                          datetime.datetime.strptime(entry_date, '%Y-%m-%d')).days, 1)
                })
                shares = 0.0
                in_position = False
                entry_date = None
        
        equity = cash + (shares * closes[i] if in_position else 0.0)
        if in_position:
            days_in_market += 1
        equity_curve.append(equity)
    
    # 最后一日平仓
    if in_position:
        px = klines[-1]['close'] * (1.0 - slippage)
        gross = shares * px
        fee = gross * commission
        proceeds = gross - fee
        cash += proceeds
        ret = (proceeds / entry_basis - 1.0) * 100 if entry_basis > 0 else 0.0
        trades.append({
            'entry': entry_date, 'exit': klines[-1]['date'],
            'entry_px': entry_price, 'exit_px': px,
            'return_pct': ret, 'reason': '期末平仓',
            'holding_days': max((datetime.datetime.strptime(klines[-1]['date'], '%Y-%m-%d') -
                                  datetime.datetime.strptime(entry_date, '%Y-%m-%d')).days, 1)
        })
        equity_curve[-1] = cash
    
    final_equity = equity_curve[-1]
    total_return = (final_equity / initial_capital - 1.0) * 100
    
    days = (datetime.datetime.strptime(klines[-1]['date'], '%Y-%m-%d') - 
            datetime.datetime.strptime(klines[0]['date'], '%Y-%m-%d')).days
    
    # 日收益率
    daily_returns = []
    for i in range(1, len(equity_curve)):
        if equity_curve[i-1] > 0:
            daily_returns.append(equity_curve[i] / equity_curve[i-1] - 1.0)
    
    wins = [t for t in trades if t['return_pct'] > 0]
    losses = [t for t in trades if t['return_pct'] <= 0]
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    avg_win = sum(t['return_pct'] for t in wins) / len(wins) if wins else 0
    avg_loss = sum(t['return_pct'] for t in losses) / len(losses) if losses else 0
    profit_factor = abs(sum(t['return_pct'] for t in wins) / sum(t['return_pct'] for t in losses)) if losses and sum(t['return_pct'] for t in losses) != 0 else None
    
    return {
        'strategy': 'v2.5趋势交易规则',
        'period': f"{klines[0]['date']} ~ {klines[-1]['date']}",
        'days': days,
        'bars': len(klines),
        'initial_capital': initial_capital,
        'final_equity': round(final_equity, 2),
        'net_profit': round(final_equity - initial_capital, 2),
        'total_return_pct': round(total_return, 2),
        'cagr_pct': round(_cagr(initial_capital, final_equity, days) * 100, 2),
        'max_drawdown_pct': round(_max_drawdown(equity_curve) * 100, 2),
        'sharpe_ratio': round(_sharpe_ratio(daily_returns), 3),
        'trade_count': len(trades),
        'win_rate_pct': round(win_rate, 1),
        'avg_win_pct': round(avg_win, 2),
        'avg_loss_pct': round(avg_loss, 2),
        'profit_factor': round(profit_factor, 2) if profit_factor else None,
        'avg_holding_days': round(sum(t['holding_days'] for t in trades) / len(trades), 1) if trades else 0,
        'exposure_pct': round(days_in_market / max(len(klines) - 1, 1) * 100, 1),
        'commission_bps': commission_bps,
        'slippage_bps': slippage_bps,
        'trades': trades,
    }


def format_backtest_summary(result):
    """格式化回测报告"""
    if 'error' in result:
        return f"❌ 回测失败：{result['error']}"
    
    lines = []
    lines.append("=" * 50)
    lines.append(f"📊 v2.5规则历史回测报告")
    lines.append(f"   周期：{result['period']}（{result['days']}天，{result['bars']}根K线）")
    lines.append("-" * 50)
    lines.append(f"💰 总收益率：{result['total_return_pct']:+.2f}%")
    lines.append(f"   年化(CAGR)：{result['cagr_pct']:+.2f}%")
    lines.append(f"   最大回撤：{result['max_drawdown_pct']:.2f}%")
    lines.append(f"   夏普比率：{result['sharpe_ratio']:.3f}")
    lines.append(f"   净利润：{result['net_profit']:+.0f}元（本金{result['initial_capital']:.0f}→{result['final_equity']:.0f}）")
    lines.append("-" * 50)
    lines.append(f"📈 交易统计（成本{result['commission_bps']}bps手续费+{result['slippage_bps']}bps滑点）")
    lines.append(f"   交易次数：{result['trade_count']}笔")
    lines.append(f"   胜率：{result['win_rate_pct']:.1f}%")
    lines.append(f"   平均盈利：{result['avg_win_pct']:+.2f}%")
    lines.append(f"   平均亏损：{result['avg_loss_pct']:+.2f}%")
    if result['profit_factor']:
        lines.append(f"   盈亏比：{result['profit_factor']:.2f}")
    lines.append(f"   平均持仓：{result['avg_holding_days']:.1f}天")
    lines.append(f"   持仓时间占比：{result['exposure_pct']:.1f}%")
    lines.append("=" * 50)
    return '\n'.join(lines)


# ============================================================
# 状态管理
# ============================================================

def migrate_from_markdown():
    if os.path.exists(STOCK_FILE):
        try:
            with open(STOCK_FILE, 'r') as f:
                content = f.read()
            position = 0
            current_cash = DEFAULT_INITIAL_CAPITAL
            cost_basis = 0
            for line in content.split('\n'):
                if '持仓：' in line:
                    try:
                        position = int(line.split('持仓：')[1].split('股')[0].strip())
                    except:
                        pass
                if '当前资金：' in line or '资金余额：' in line:
                    try:
                        cash_str = line.split('：')[1].split(' ')[0].replace(',', '').replace('RMB', '').strip()
                        current_cash = int(float(cash_str))
                    except:
                        pass
                if '持仓成本：' in line and '—' not in line:
                    try:
                        cost_str = line.split('持仓成本：')[1].strip().rstrip('元')
                        if cost_str and cost_str != '—':
                            cost_basis = float(cost_str)
                    except:
                        pass
            return {
                'stock_code': DEFAULT_STOCK_CODE,
                'stock_name': '盈峰环境',
                'initial_cash': DEFAULT_INITIAL_CAPITAL,
                'current_cash': current_cash,
                'position': position,
                'cost_basis': cost_basis,
                'max_price': 0,
                'buy_date': None,
                'start_date': '2026-05-12',
                'last_update': datetime.datetime.now().strftime('%Y-%m-%d'),
                'trade_history': [],
                'total_loss': current_cash - DEFAULT_INITIAL_CAPITAL,
                'loss_rate': (current_cash - DEFAULT_INITIAL_CAPITAL) / DEFAULT_INITIAL_CAPITAL * 100,
                'note': '从Markdown迁移'
            }
        except Exception as e:
            print(f"迁移失败: {e}")
    return None


def read_position():
    if os.path.exists(STOCK_STATE_FILE):
        try:
            with open(STOCK_STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    migrated = migrate_from_markdown()
    if migrated:
        save_position(migrated)
        return migrated
    return {
        'stock_code': DEFAULT_STOCK_CODE,
        'stock_name': '盈峰环境',
        'initial_cash': DEFAULT_INITIAL_CAPITAL,
        'current_cash': DEFAULT_INITIAL_CAPITAL,
        'position': 0,
        'cost_basis': 0,
        'max_price': 0,
        'buy_date': None,
        'last_sell_date': None,
        'cooldown_until': None,
        'start_date': datetime.datetime.now().strftime('%Y-%m-%d'),
        'last_update': datetime.datetime.now().strftime('%Y-%m-%d'),
        'trade_history': [],
        'total_loss': 0,
        'loss_rate': 0,
        'note': ''
    }


def save_position(state):
    os.makedirs(os.path.dirname(STOCK_STATE_FILE), exist_ok=True)
    with open(STOCK_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def update_max_price(state, current_price):
    if state.get('position', 0) > 0:
        state['max_price'] = max(state.get('max_price', 0), current_price)
    return state


def execute_trade(action, price, state, indicators=None):
    position = state.get('position', 0)
    cash = state.get('current_cash', state.get('cash', DEFAULT_INITIAL_CAPITAL))

    if action == "买入":
        shares = int(cash / price / 100) * 100
        if shares > 0:
            return {
                'action': '买入',
                'shares': shares,
                'price': price,
                'amount': shares * price,
                'new_position': position + shares,
                'new_cash': cash - shares * price
            }
        return None

    elif action == "卖出" and position > 0:
        proceeds = position * price
        today = datetime.datetime.now()
        cooldown = (today + datetime.timedelta(days=3)).strftime('%Y-%m-%d')
        return {
            'action': '卖出',
            'shares': position,
            'price': price,
            'amount': proceeds,
            'new_position': 0,
            'new_cash': cash + proceeds,
            'last_sell_date': today.strftime('%Y-%m-%d'),
            'cooldown_until': cooldown
        }
    return None


def update_stock_file(data, action, trade_info, decision_reason, indicators=None):
    state = read_position()
    today = datetime.datetime.now().strftime('%Y-%m-%d')

    if state.get('position', 0) > 0:
        state = update_max_price(state, data['current_price'])

    if trade_info and 'action' in trade_info:
        trade = {
            'date': today,
            'action': trade_info['action'],
            'price': trade_info['price'],
            'shares': trade_info['shares'],
            'reason': decision_reason
        }
        state['trade_history'].append(trade)
        if trade_info['action'] == '买入':
            state['position'] = trade_info['new_position']
            state['current_cash'] = trade_info['new_cash']
            state['cost_basis'] = trade_info['price']
            state['buy_date'] = today
            state['max_price'] = trade_info['price']
            state['last_sell_date'] = None
            state['cooldown_until'] = None
        elif trade_info['action'] == '卖出':
            realized = trade_info['new_cash'] - state['initial_cash']
            state['position'] = 0
            state['current_cash'] = trade_info['new_cash']
            state['cost_basis'] = 0
            state['buy_date'] = None
            state['max_price'] = 0
            state['last_sell_date'] = trade_info.get('last_sell_date', today)
            state['cooldown_until'] = trade_info.get('cooldown_until', today)
            state['total_loss'] = realized
            state['loss_rate'] = realized / state['initial_cash'] * 100

    state['last_update'] = today
    save_position(state)

    position = state.get('position', 0)
    cash = state.get('current_cash', state.get('cash', DEFAULT_INITIAL_CAPITAL))
    cost_basis = state.get('cost_basis', 0)
    cooldown = state.get('cooldown_until', '')

    trade_table = "| 日期 | 操作 | 价格 | 数量 | 金额 | 备注 |\n|------|------|------|------|------|------|\n"
    for t in state.get('trade_history', []):
        if t['action'] in ['买入', '卖出']:
            trade_table += f"| {t['date']} | {t['action']} | {t['price']:.2f} | {t['shares']} | {t.get('amount', 0):.0f} | {t['reason']} |\n"

    if position > 0:
        unrealized = (data['current_price'] - cost_basis) * position
        unrealized_rate = (data['current_price'] - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0
        pnl = f"浮盈：{unrealized:.0f}元 ({unrealized_rate:+.1f}%)" if unrealized >= 0 else f"浮亏：{abs(unrealized):.0f}元 ({unrealized_rate:.1f}%)"
    else:
        realized = cash - state['initial_cash']
        pnl = f"已实现盈亏：{realized:.0f}元 ({realized/state['initial_cash']*100:+.1f}%)"

    ind_str = ""
    if indicators:
        ind_str = f"""
### 技术指标
- MA5：{indicators['ma5']:.3f} | MA10：{indicators['ma10']:.3f} | MA20：{indicators['ma20']:.3f}{f" | MA60：{indicators['ma60']:.3f}" if indicators['ma60'] else ""}
- 20日最高：{indicators['high_20']:.3f}，回撤：{indicators['drop_from_high']:.1f}%
- 持仓盈亏：{indicators['total_gain']:+.1f}% | 最大回撤：{indicators['drawdown_from_max']:.1f}%
- MA空头排列：{'⚠️是' if indicators['ma_bearish'] else '否'}
- RSI(14)：{indicators['rsi14']:.1f} | {indicators['rsi_signal']}
- 成交量比：{indicators['vol_ratio']:.2f}倍 | 连续下跌：{indicators['consecutive_down']}天
- MA5方向向上：{indicators['ma5_up']}
"""
    cooldown_str = f"\n- **冷却期**：禁止买入至 {cooldown}" if cooldown else ""

    content = f"""# 模拟交易记录 - {state.get('stock_name', '盈峰环境')} ({state.get('stock_code', '000967')})

## 📊 持仓现状
- **持仓**：{position}股
- **当前资金**：{cash:.0f} RMB
- **持仓成本**：{cost_basis:.2f}元
- **持仓最高**：{state.get('max_price', 0):.2f}元
- **起始资金**：{state['initial_cash']} RMB
- **最后更新**：{today}{cooldown_str}

## 📈 盈亏状态
{pnl}

## 📋 交易历史
{trade_table}
{ind_str}
---

## 📝 当前分析
### {today}
- **数据来源**：{data.get('source', '未知')}
- **当前价**：{data.get('current_price', 0)}元
- **今日建议**：{action}
- **理由**：{decision_reason}
"""
    os.makedirs(os.path.dirname(STOCK_FILE), exist_ok=True)
    with open(STOCK_FILE, 'w') as f:
        f.write(content)
    return True


def main(stock_code=DEFAULT_STOCK_CODE, backtest_only=False):
    if backtest_only:
        # 纯回测模式：从同花顺获取数据，回测v2.5规则
        result = get_stock_data(stock_code)
        data, err = result
        if not data:
            print(f"获取数据失败: {err}")
            return
        if len(data['klines']) < 30:
            print(f"数据不足：只有{len(data['klines'])}根K线，需要至少30根")
            return
        result = backtest_v25_rules(data['klines'], initial_capital=DEFAULT_INITIAL_CAPITAL)
        print(format_backtest_summary(result))
        return

    print(f"=== 模拟操盘手 v2.5 ===")
    print(f"股票代码: {stock_code}\n")

    # 1. 获取数据
    print("【1. 获取数据】")
    result = get_stock_data(stock_code)
    data, err = result
    if not data:
        print(f"获取数据失败: {err}")
        return
    print(f"数据来源: {data['source']} | 名称: {data['name']} | 价格: {data['current_price']}\n")

    # 1.5 回测报告（同时输出）
    print("【1.5 历史回测】")
    if len(data['klines']) >= 30:
        bt = backtest_v25_rules(data['klines'], initial_capital=DEFAULT_INITIAL_CAPITAL)
        print(format_backtest_summary(bt))
    else:
        print(f"  K线不足（{len(data['klines'])}根），跳过回测")
    print()

    # 2. 读取持仓
    print("【2. 读取持仓】")
    state = read_position()
    cooldown = state.get('cooldown_until', '')
    print(f"持仓: {state['position']}股 | 资金: {state['current_cash']:.0f}元 | 成本: {state['cost_basis']:.3f}元")
    print(f"持仓最高: {state['max_price']:.3f}元")
    if cooldown:
        print(f"⚠️ 冷却期：禁止买入至 {cooldown}")
    print()

    # 3. 技术分析
    print("【3. 技术指标】")
    indicators = calc_all_indicators(data, state['position'], state)
    if indicators:
        for line in format_indicators(indicators).split('\n'):
            print(f"  {line}")
    print()

    # 4. 趋势决策
    print("【4. 趋势决策】")
    action, reason, indicators = trend_decide(data, state['position'], state)
    print(f"建议: {action} | {reason}\n")

    # 5. 执行交易
    print("【5. 执行交易】")
    trade_info = execute_trade(action, data['current_price'], state, indicators)
    if trade_info:
        print(f"执行: {trade_info['action']} {trade_info['shares']}股 @ {trade_info['price']}")
        print(f"新持仓: {trade_info['new_position']}股 | 新资金: {trade_info['new_cash']:.0f}元")
        if trade_info['action'] == '卖出':
            print(f"⏳ 冷却期至: {trade_info.get('cooldown_until', '')}")
    else:
        print("无需执行")
        trade_info = {
            'new_position': state['position'],
            'new_cash': state.get('current_cash', DEFAULT_INITIAL_CAPITAL)
        }
    print()

    # 6. 更新文件
    print("【6. 更新状态】")
    update_stock_file(data, action, trade_info, reason, indicators)
    print("状态文件已更新\n")

    # 输出摘要
    print("=" * 50)
    final = read_position()
    print(f"【数据来源】{data['source']}")
    print(f"【当前股价】{data['current_price']}元")
    print(f"【今日建议】{action}")
    print(f"【操作执行】{'已执行' if trade_info and 'action' in trade_info else '未执行'}")
    print(f"【当前持仓】{final['position']}股")
    print(f"【资金余额】{final['current_cash']:.0f}元")
    if final['position'] > 0 and indicators:
        profit = (data['current_price'] - final['cost_basis']) * final['position']
        print(f"【持仓盈亏】{'↑' if profit >= 0 else '↓'}浮盈{abs(profit):.0f}元 ({indicators['total_gain']:+.1f}%)")
        print(f"【持仓最高】{final['max_price']:.3f}元 | 回撤{indicators['drawdown_from_max']:.1f}%")
    if final.get('cooldown_until'):
        print(f"【冷却期】禁止买入至 {final['cooldown_until']}")
    print("=" * 50)

    print("\n# AI记忆摘要")
    print(f"CURRENT_POSITION={final['position']}")
    print(f"CURRENT_CASH={final['current_cash']:.0f}")
    print(f"COST_BASIS={final['cost_basis']}")
    print(f"MAX_PRICE={final['max_price']}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if '--backtest' in args:
        args.remove('--backtest')
        code = args[0] if args else DEFAULT_STOCK_CODE
        main(code, backtest_only=True)
    else:
        code = args[0] if args else DEFAULT_STOCK_CODE
        main(code)
