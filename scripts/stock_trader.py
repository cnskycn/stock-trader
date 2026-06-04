#!/usr/bin/env python3
"""
模拟操盘手 - 核心脚本
支持多数据源：同花顺、新浪财经、腾讯财经
"""

import os
import sys
import json
import datetime
import requests
from pathlib import Path

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
DEFAULT_STOCK_CODE = "000967"  # 盈峰环境
DEFAULT_INITIAL_CAPITAL = 100000  # 10万本金


def get_data_from_ths(stock_code):
    """数据源1: 同花顺"""
    if not THSDK_AVAILABLE:
        return None, "同花顺未安装"
    
    try:
        with THS() as ths:
            # 搜索股票 - Response对象
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
            
            # 获取K线 - 也是Response对象
            klines_result = ths.klines(ths_code, interval="day", count=20)
            if not klines_result or not hasattr(klines_result, 'data') or not klines_result.data:
                return None, "无K线数据"
            
            data = []
            for item in klines_result.data:
                # 处理时间格式
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
            
            # 最新价
            latest = data[-1]
            current_price = latest.get('close', 0)
            
            return {
                'source': '同花顺',
                'name': name,
                'current_price': current_price,
                'klines': data
            }, None
    except Exception as e:
        return None, f"同花顺错误: {str(e)}"


def get_data_from_sina(stock_code):
    """数据源2: 新浪财经"""
    try:
        code = 'sz' + stock_code if len(stock_code) == 6 else stock_code
        
        # 实时行情
        url = f'https://hq.sinajs.cn/list={code}'
        headers = {'Referer': 'https://finance.sina.com.cn'}
        resp = requests.get(url, headers=headers, timeout=10)
        data = resp.text.split('=')[1].strip('"').split(',')
        
        current_price = float(data[3])
        open_price = float(data[1])
        high = float(data[4])
        low = float(data[5])
        volume = int(data[8])
        name = data[0]
        
        # K线
        url2 = f'https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData?symbol={code}&scale=240&ma=5&datalen=20'
        klines = json.loads(requests.get(url2, timeout=10).text)
        
        kline_data = []
        for k in klines:
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
            'open': open_price,
            'high': high,
            'low': low,
            'volume': volume,
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
            'change': float(fields[4]),
            'change_pct': float(fields[5])
        }, None
    except Exception as e:
        return None, f"腾讯错误: {str(e)}"


def get_stock_data(stock_code):
    """多数据源获取股票数据"""
    # 尝试同花顺
    result = get_data_from_ths(stock_code)
    if result[0]:
        return result
    
    # 尝试新浪
    result = get_data_from_sina(stock_code)
    if result[0]:
        return result
    
    # 尝试腾讯
    result = get_data_from_tencent(stock_code)
    if result[0]:
        return result
    
    return None, "所有数据源均失败"


def analyze_and_decide(data, position=0):
    """分析并决策"""
    current_price = data['current_price']
    klines = data.get('klines', [])
    
    if not klines:
        # 没有K线数据，无法分析
        return "观望", "无足够数据进行分析"
    
    # 找20日最高价
    high_20 = max(k['high'] for k in klines)
    
    # 计算跌幅
    drop_pct = (high_20 - current_price) / high_20 * 100
    
    # 止跌信号分析
    recent = klines[-5:] if len(klines) >= 5 else klines
    
    # 检查成交量萎缩
    volume_shrinking = False
    if len(recent) >= 2:
        if recent[-1].get('volume', 0) < recent[-2].get('volume', 0) * 0.7:
            volume_shrinking = True
    
    # 检查跌幅收窄
    narrowing_drop = False
    if len(recent) >= 2:
        drop_today = (recent[-1]['close'] - recent[-2]['close']) / recent[-2]['close'] * 100
        drop_yesterday = (recent[-2]['close'] - recent[-3]['close']) / recent[-3]['close'] * 100 if len(recent) >= 3 else 0
        if abs(drop_today) < abs(drop_yesterday):
            narrowing_drop = True
    
    # 检查十字星
    star_candle = False
    if len(recent) >= 1:
        last = recent[-1]
        body = abs(last['close'] - last['open'])
        shadow = (last['high'] - last['low']) - body
        if body < (last['high'] - last['low']) * 0.3:
            star_candle = True
    
    # 决策逻辑
    if position > 0:
        # 持有股票
        if drop_pct > 15:
            return "卖出", f"从最高点下跌{drop_pct:.1f}%，触发止损"
        else:
            return "观望", f"持有中，下跌{drop_pct:.1f}%，未到止损点"
    else:
        # 空仓
        if drop_pct > 20:
            # 激进策略：超跌买入
            signals = []
            if volume_shrinking:
                signals.append("成交量萎缩")
            if narrowing_drop:
                signals.append("跌幅收窄")
            if star_candle:
                signals.append("十字星")
            
            if signals:
                return "买入", f"从最高点下跌{drop_pct:.1f}%，出现止跌信号: {'+'.join(signals)}"
            elif drop_pct > 30:
                return "买入", f"从最高点下跌{drop_pct:.1f}%，超跌严重"
        
        return "观望", f"从最高点下跌{drop_pct:.1f}%，未到买入阈值"


def migrate_from_markdown():
    """从Markdown迁移数据到JSON"""
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
                'note': '从Markdown迁移的数据'
            }
        except Exception as e:
            print(f"迁移失败: {e}")
    return None


def read_position():
    """读取当前持仓状态 - 使用JSON文件"""
    # 首先检查JSON文件
    if os.path.exists(STOCK_STATE_FILE):
        try:
            with open(STOCK_STATE_FILE, 'r') as f:
                return json.load(f)
        except:
            pass
    
    # JSON不存在，尝试从Markdown迁移
    migrated = migrate_from_markdown()
    if migrated:
        save_position(migrated)
        print(f"✅ 已从Markdown迁移数据到JSON")
        print(f"   持仓: {migrated['position']}股")
        print(f"   资金: {migrated['current_cash']}元")
        return migrated
    
    # 如果都失败，返回默认值
    return {
        'stock_code': DEFAULT_STOCK_CODE,
        'stock_name': '盈峰环境',
        'initial_cash': DEFAULT_INITIAL_CAPITAL,
        'current_cash': DEFAULT_INITIAL_CAPITAL,
        'position': 0,
        'cost_basis': 0,
        'max_price': 0,
        'buy_date': None,
        'start_date': datetime.datetime.now().strftime('%Y-%m-%d'),
        'last_update': datetime.datetime.now().strftime('%Y-%m-%d'),
        'trade_history': [],
        'total_loss': 0,
        'loss_rate': 0,
        'note': ''
    }


def execute_trade(action, price, state):
    """执行交易"""
    position = state.get('position', 0)
    cash = state.get('current_cash', state.get('cash', DEFAULT_INITIAL_CAPITAL))
    
    if action == "买入":
        # 全仓买入
        shares = int(cash / price / 100) * 100  # 取整到100股
        if shares > 0:
            cost = shares * price
            new_position = position + shares
            new_cash = cash - cost
            return {
                'action': '买入',
                'shares': shares,
                'price': price,
                'amount': cost,
                'new_position': new_position,
                'new_cash': new_cash
            }
        return None
    
    elif action == "卖出" and position > 0:
        # 清仓
        proceeds = position * price
        return {
            'action': '卖出',
            'shares': position,
            'price': price,
            'amount': proceeds,
            'new_position': 0,
            'new_cash': cash + proceeds
        }
    
    return None


def save_position(state):
    """保存持仓状态到JSON文件"""
    os.makedirs(os.path.dirname(STOCK_STATE_FILE), exist_ok=True)
    with open(STOCK_STATE_FILE, 'w') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def update_stock_file(data, action, trade_info, decision_reason):
    """更新状态文件 - 同时更新JSON和Markdown"""
    state = read_position()
    today = datetime.datetime.now().strftime('%Y-%m-%d')
    
    # 如果有实际交易，更新状态
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
        elif trade_info['action'] == '卖出':
            state['position'] = 0
            state['current_cash'] = trade_info['new_cash']
            state['cost_basis'] = 0
            state['buy_date'] = None
            state['total_loss'] = trade_info['new_cash'] - state['initial_cash']
            state['loss_rate'] = state['total_loss'] / state['initial_cash'] * 100
    
    state['last_update'] = today
    
    # 保存JSON文件
    save_position(state)
    
    # 更新Markdown文件（保持可读性）
    position = state.get('position', 0)
    cash = state.get('current_cash', state.get('cash', DEFAULT_INITIAL_CAPITAL))
    cost_basis = state.get('cost_basis', 0)
    
    # 构建交易历史表格
    trade_table = "| 日期 | 操作 | 价格 | 数量 | 金额 | 备注 |\n|------|------|------|------|------|------|\n"
    for t in state.get('trade_history', []):
        if t['action'] in ['买入', '卖出']:
            trade_table += f"| {t['date']} | {t['action']} | {t['price']:.2f} | {t['shares']} | {t.get('amount', 0):.0f} | {t['reason']} |\n"
        else:
            trade_table += f"| {t['date']} | {t['action']} | - | - | - | {t['reason']} |\n"
    
    # 计算盈亏
    if position > 0:
        current_price = data.get('current_price', 0)
        unrealized_pnl = (current_price - cost_basis) * position
        unrealized_pnl_rate = (current_price - cost_basis) / cost_basis * 100 if cost_basis > 0 else 0
        pnl_text = f"浮盈：{unrealized_pnl:.0f}元 ({unrealized_pnl_rate:.1f}%)"
    else:
        realized_pnl = cash - state['initial_cash']
        pnl_text = f"已实现盈亏：{realized_pnl:.0f}元 ({realized_pnl/state['initial_cash']*100:.1f}%)"
    
    content = f"""# 模拟交易记录 - {state.get('stock_name', '盈峰环境')} ({state.get('stock_code', '000967')})

## 📊 持仓现状
- **持仓**：{position}股
- **当前资金**：{cash:.0f} RMB
- **持仓成本**：{cost_basis:.2f}元
- **起始资金**：{state['initial_cash']} RMB
- **起始日期**：{state.get('start_date', '2026-05-12')}
- **最后更新**：{today}

## 📈 盈亏状态
{pnl_text}

## 📋 交易历史
{trade_table}

---

## 📝 当前分析

### {today}（今日）
- **数据来源**：{data.get('source', '未知')}
- **当前价**：{data.get('current_price', 0)}元
- **今日建议**：{action}
- **理由**：{decision_reason}
"""
    
    os.makedirs(os.path.dirname(STOCK_FILE), exist_ok=True)
    with open(STOCK_FILE, 'w') as f:
        f.write(content)
    
    return True


def main(stock_code=DEFAULT_STOCK_CODE):
    """主函数"""
    print(f"=== 模拟操盘手 ===")
    print(f"股票代码: {stock_code}")
    print()
    
    # 1. 获取数据
    print("【1. 获取数据】")
    result = get_stock_data(stock_code)
    data = result[0]
    err = result[1]
    
    if not data:
        print(f"获取数据失败: {err}")
        return
    
    print(f"数据来源: {data['source']}")
    print(f"股票名称: {data['name']}")
    print(f"当前价格: {data['current_price']}")
    print()
    
    # 2. 读取持仓
    print("【2. 读取持仓】")
    state = read_position()
    print(f"股票代码: {state.get('stock_code', DEFAULT_STOCK_CODE)}")
    print(f"股票名称: {state.get('stock_name', '盈峰环境')}")
    print(f"当前持仓: {state.get('position', 0)}股")
    print(f"资金余额: {state.get('current_cash', state.get('cash', DEFAULT_INITIAL_CAPITAL))}元")
    print(f"持仓成本: {state.get('cost_basis', 0):.2f}元")
    print()
    
    # 3. 分析决策
    print("【3. 分析决策】")
    action, reason = analyze_and_decide(data, state['position'])
    print(f"建议操作: {action}")
    print(f"理由: {reason}")
    print()
    
    # 4. 执行交易
    print("【4. 执行交易】")
    trade_info = execute_trade(action, data['current_price'], state)
    if trade_info:
        print(f"执行成功: {trade_info['action']} {trade_info['shares']}股 @ {trade_info['price']}")
        print(f"新持仓: {trade_info['new_position']}股")
        print(f"新资金: {trade_info['new_cash']}元")
    else:
        print("无需执行交易")
        trade_info = {
            'new_position': state['position'],
            'new_cash': state.get('current_cash', state.get('cash', DEFAULT_INITIAL_CAPITAL))
        }
    print()
    
    # 5. 更新文件
    print("【5. 更新状态】")
    update_stock_file(data, action, trade_info, reason)
    print("状态文件已更新")
    
    # 输出结果
    print()
    print("=" * 40)
    print(f"【数据来源】{data['source']}")
    print(f"【当前股价】{data['current_price']}元")
    print(f"【今日建议】{action}")
    
    # 检查是否实际执行了交易
    executed = trade_info and 'action' in trade_info
    print(f"【操作执行】{'已执行' if executed else '未执行'}")
    
    # 读取最新状态
    final_state = read_position()
    print(f"【当前持仓】{final_state.get('position', 0)}股")
    print(f"【资金余额】{final_state.get('current_cash', DEFAULT_INITIAL_CAPITAL)}元")
    
    # 显示持仓提醒
    if final_state.get('position', 0) > 0:
        profit = (data['current_price'] - final_state.get('cost_basis', 0)) * final_state['position']
        profit_rate = (data['current_price'] - final_state.get('cost_basis', 0)) / final_state.get('cost_basis', 1) * 100
        print(f"【持仓盈亏】浮盈{profit:.0f}元 ({profit_rate:.1f}%)" if profit >= 0 else f"【持仓盈亏】浮亏{abs(profit):.0f}元 ({profit_rate:.1f}%)")
    
    print(f"【状态文件】已更新")
    print("=" * 40)
    
    # 打印持仓现状摘要（供AI记忆使用）
    print()
    print("# AI记忆摘要")
    print(f"CURRENT_POSITION={final_state.get('position', 0)}")
    print(f"CURRENT_CASH={final_state.get('current_cash', DEFAULT_INITIAL_CAPITAL)}")
    print(f"COST_BASIS={final_state.get('cost_basis', 0)}")


if __name__ == "__main__":
    code = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_STOCK_CODE
    main(code)
