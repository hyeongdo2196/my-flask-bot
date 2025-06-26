import os
import time
import hmac
import hashlib
import requests
import json
import uuid
import threading
import decimal
from flask import Flask, request, jsonify
import traceback

app = Flask(__name__)

API_KEY = os.environ.get('BYBIT_API_KEY')
API_SECRET = os.environ.get('BYBIT_API_SECRET')
BASE_URL = 'https://api.bybit.com'

TRADE_LEVERAGE = 10
MY_RISK_RATIO = 0.10
TRADE_MARGIN_MODE = 'ISOLATED'

TP_PROFIT_RATE = 0.06   # 목표수익률 +7%
SL_LOSS_RATE   = -0.02  # 손절수익률 -2% (음수)
COMMISSION = 0.0006     # 왕복수수료(0.03%*2*레버)

SYMBOL_POLICY = {
    'BTCUSDT': {
        'tp': 0.06,
        'sl': -0.02,
        'trailing_steps': [
            {'trigger': 0.05, 'sl': 0.03},
            {'trigger': 0.07, 'sl': 0.05},
        ],
    },
    'ETHUSDT': {
        'tp': 0.06,
        'sl': -0.02,
        'trailing_steps': [
            {'trigger': 0.05, 'sl': 0.03},
            {'trigger': 0.07, 'sl': 0.05},
        ],
    },
    'DOGEUSDT': {
        'tp': 0.1,
        'sl': -0.02,
        'trailing_steps': [
            {'trigger': 0.05, 'sl': 0.03},
            {'trigger': 0.07, 'sl': 0.05},
        ],
    }
}

def get_precision_from_step(step):
    try:
        if float(step) == 1:
            return 0
        else:
            return abs(decimal.Decimal(str(step)).as_tuple().exponent)
    except Exception:
        return 0

def get_symbol_policy(symbol):
    base_symbol = get_underlying_symbol(symbol)
    return SYMBOL_POLICY.get(base_symbol, {
        'tp': TP_PROFIT_RATE,
        'sl': SL_LOSS_RATE,
        'trailing_steps': None
    })

def update_symbol_meta():
    endpoint = '/v5/market/instruments-info'
    params = {'category': 'linear'}
    resp = http_request('GET', endpoint, params)
    try:
        data = resp.json()
        meta = {}
        if data.get('retCode') == 0:
            for item in data['result']['list']:
                sym = item['symbol']
                lot = item['lotSizeFilter']
                step = float(lot['qtyStep'])
                min_qty = float(lot['minOrderQty'])
                max_qty = float(lot['maxOrderQty'])
                max_mkt_qty = float(lot.get('maxMktOrderQty', max_qty))
                contract_size = float(item.get('contractSize', 1.0))
                precision = abs(decimal.Decimal(str(step)).as_tuple().exponent)
                meta[sym] = {
                    "step_size": step,
                    "precision": precision,
                    "min_qty": min_qty,
                    "max_qty": max_qty,
                    "max_mkt_qty": max_mkt_qty,
                    "contract_size": contract_size
                }
        return meta
    except Exception as e:
        print("심볼 메타 정보 조회 오류:", e, flush=True)
        return {}

def refresh_symbol_meta():
    global SYMBOL_META, SYMBOL_PRECISION, SYMBOL_STEP_SIZE, SYMBOL_MIN_QTY, SYMBOL_MAX_QTY, SYMBOL_MAX_MKT_QTY, SYMBOL_CONTRACT_SIZE, SYMBOL_TICK_SIZE
    SYMBOL_META = update_symbol_meta()
    SYMBOL_PRECISION = {k: v["precision"] for k, v in SYMBOL_META.items()}
    SYMBOL_STEP_SIZE = {k: v["step_size"] for k, v in SYMBOL_META.items()}
    SYMBOL_MIN_QTY   = {k: v["min_qty"] for k, v in SYMBOL_META.items()}
    SYMBOL_MAX_QTY   = {k: v["max_qty"] for k, v in SYMBOL_META.items()}
    SYMBOL_MAX_MKT_QTY = {k: v["max_mkt_qty"] for k, v in SYMBOL_META.items()}
    SYMBOL_CONTRACT_SIZE = {k: v["contract_size"] for k, v in SYMBOL_META.items()}
    SYMBOL_TICK_SIZE = {k: v.get("step_size", 0.01) for k, v in SYMBOL_META.items()}

def get_underlying_symbol(symbol):
    return symbol.replace('.P', '')

def get_timestamp():
    return str(int(time.time() * 1000))

def generate_signature(timestamp, api_key, recv_window, body, api_secret):
    pre_hash = str(timestamp) + api_key + str(recv_window) + body
    return hmac.new(api_secret.encode('utf-8'), pre_hash.encode('utf-8'), hashlib.sha256).hexdigest()

def http_request(method, endpoint, body_dict):
    timestamp = get_timestamp()
    recv_window = 5000
    api_key = API_KEY
    if method == "GET":
        params_sorted = "&".join(f"{k}={body_dict[k]}" for k in sorted(body_dict.keys())) if body_dict else ""
        sign_body = params_sorted
    else:
        sign_body = json.dumps(body_dict) if body_dict else ""
    sign = generate_signature(timestamp, api_key, recv_window, sign_body, API_SECRET)
    headers = {
        'X-BAPI-API-KEY': api_key,
        'X-BAPI-SIGN': sign,
        'X-BAPI-TIMESTAMP': timestamp,
        'X-BAPI-RECV-WINDOW': str(recv_window),
        'Content-Type': 'application/json'
    }
    url = BASE_URL + endpoint
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, params=body_dict)
        else:
            resp = requests.post(url, headers=headers, data=sign_body)
        print(f"[HTTP] {method} {url} params/body: {body_dict} --> status:{resp.status_code}", flush=True)
        print(f"[HTTP] Response: {resp.text}", flush=True)
        return resp
    except Exception as e:
        print(f"[HTTP ERROR] {method} {url} : {e}", flush=True)
        raise

refresh_symbol_meta()

def get_my_balance():
    endpoint = '/v5/account/wallet-balance'
    params = {'accountType': 'UNIFIED'}
    resp = http_request('GET', endpoint, params)
    try:
        js = resp.json()
        print(f"[잔고 응답] {js}", flush=True)
        if js.get('retCode') == 0:
            wallets = js['result']['list'][0]['coin']
            for coin in wallets:
                if coin['coin'] == 'USDT':
                    if 'walletBalance' in coin:
                        return float(coin['walletBalance'])
    except Exception:
        print("[잔고 조회 실패]", flush=True)
    return 0.0

def set_leverage_and_mode(symbol, buy_leverage, sell_leverage, margin_mode):
    endpoint = '/v5/position/set-leverage'
    body = {
        "category": "linear",
        "symbol": symbol,
        "buyLeverage": str(buy_leverage),
        "sellLeverage": str(sell_leverage),
        "marginMode": margin_mode,
    }
    resp = http_request('POST', endpoint, body)
    try:
        data = resp.json()
        print(f"[레버리지 설정 응답] {data}", flush=True)
        return data.get('retCode') == 0
    except Exception:
        print("[레버리지 설정 실패]", flush=True)
        return False

def get_position_size(symbol, position_idx):
    endpoint = '/v5/position/list'
    body = {"category": "linear", "symbol": symbol}
    resp = http_request('GET', endpoint, body)
    try:
        data = resp.json()
        print(f"[포지션 사이즈 응답] {data}", flush=True)
        if data.get('retCode') == 0:
            pos_list = data['result']['list']
            for pos in pos_list:
                if int(pos.get('positionIdx', 0)) == position_idx:
                    return float(pos.get('size', 0))
    except Exception:
        print("[포지션 사이즈 조회 실패]", flush=True)
    return 0

def get_position_entry_price(symbol, position_idx):
    endpoint = '/v5/position/list'
    body = {"category": "linear", "symbol": symbol}
    resp = http_request('GET', endpoint, body)
    try:
        data = resp.json()
        print(f"[포지션 진입가 응답] {data}", flush=True)
        if data.get('retCode') == 0:
            pos_list = data['result']['list']
            for pos in pos_list:
                if int(pos.get('positionIdx', 0)) == position_idx:
                    price = pos.get('avgPrice', pos.get('entryPrice', None))
                    if price is not None:
                        return float(price)
    except Exception:
        print("[포지션 진입가 조회 실패]", flush=True)
    return None

def has_open_position(symbol, position_idx):
    return get_position_size(symbol, position_idx) > 0

def adjust_qty(symbol, qty, order_type="Market"):
    symbol = get_underlying_symbol(symbol)
    step = SYMBOL_STEP_SIZE.get(symbol, 1)
    precision = SYMBOL_PRECISION.get(symbol, 0)
    min_qty = SYMBOL_MIN_QTY.get(symbol, 1)
    if order_type == "Market":
        max_qty = SYMBOL_MAX_MKT_QTY.get(symbol, 71000)
    else:
        max_qty = SYMBOL_MAX_QTY.get(symbol, 710000)
    qty = max(qty, min_qty)
    qty = min(qty, max_qty)
    if precision == 0:
        qty = int(qty // step * step)
    else:
        qty = round((qty // step) * step, precision)
    qty = max(min_qty, min(qty, max_qty))
    if precision == 0:
        qty = int(qty)
    return qty

def get_qty_str(symbol, qty):
    precision = SYMBOL_PRECISION.get(symbol, 0)
    if precision == 0:
        return str(int(round(qty)))
    else:
        fmt = f"{{:.{precision}f}}"
        return fmt.format(qty)

def get_order_qty(symbol, order_type="Market"):
    meta_symbol = get_underlying_symbol(symbol)
    price_endpoint = '/v5/market/tickers'
    price_body = {'category': 'linear', 'symbol': meta_symbol}
    price_resp = http_request('GET', price_endpoint, price_body)
    price = None
    try:
        js = price_resp.json()
        print(f"[현재가 응답] {js}", flush=True)
        price = float(js['result']['list'][0]['lastPrice'])
    except Exception:
        print("[현재가 조회 실패]", flush=True)
        price = None

    my_balance = get_my_balance()
    contract_size = SYMBOL_CONTRACT_SIZE.get(meta_symbol, 1.0)
    if order_type == "Market":
        max_qty = SYMBOL_MAX_MKT_QTY.get(meta_symbol, 71000)
    else:
        max_qty = SYMBOL_MAX_QTY.get(meta_symbol, 710000)
    if price and my_balance:
        available_usdt = my_balance * TRADE_LEVERAGE * MY_RISK_RATIO
        raw_qty = available_usdt / (price * contract_size)
        qty = adjust_qty(meta_symbol, raw_qty, order_type)
    else:
        precision = SYMBOL_PRECISION.get(meta_symbol, 3)
        qty = 1 if precision == 0 else round(1.0, precision)
    if qty > max_qty:
        qty = max_qty
    print(f"[주문수량 계산] price:{price}, balance:{my_balance}, qty:{qty}", flush=True)
    return qty

def close_position_and_wait(symbol, close_side, max_retry=3, wait_sec=5):
    symbol = get_underlying_symbol(symbol)
    position_idx = 1 if close_side == 'Buy' else 2
    qty = get_position_size(symbol, position_idx)
    if qty == 0:
        return True
    for retry in range(1, max_retry + 1):
        qty = get_position_size(symbol, position_idx)
        if qty == 0:
            return True
        qty_str = get_qty_str(symbol, qty)
        endpoint = '/v5/order/create'
        body = {
            'category': 'linear',
            'symbol': symbol,
            'side': 'Sell' if close_side == 'Buy' else 'Buy',
            'orderType': 'Market',
            'reduceOnly': True,
            'qty': qty_str,
            'positionIdx': position_idx,
            'orderLinkId': f"close_{uuid.uuid4().hex}"
        }
        print("[포지션 종료 요청]", body, flush=True)
        http_request('POST', endpoint, body)
        for _ in range(wait_sec):
            time.sleep(1)
            remain = get_position_size(symbol, position_idx)
            if remain == 0:
                return True
    return False

def wait_until_position_open(symbol, position_idx, timeout=10, interval=0.5):
    start = time.time()
    while time.time() - start < timeout:
        size = get_position_size(symbol, position_idx)
        if size > 0:
            return size
        time.sleep(interval)
    return 0

def round_to_tick(price, tick):
    decimals = abs(decimal.Decimal(str(tick)).as_tuple().exponent)
    return float(round(price / tick) * tick)

def enforce_min_tick_gap(entry, tgt, tick, min_gap=20):
    gap = abs(tgt - entry)
    min_dist = tick * min_gap
    if gap < min_dist:
        if tgt > entry:
            tgt = entry + min_dist
        else:
            tgt = entry - min_dist
    return round_to_tick(tgt, tick)

def get_tp_sl_by_real_pnl(entry_price, position_idx, lev, tp_pnl=TP_PROFIT_RATE, sl_pnl=SL_LOSS_RATE, commission=COMMISSION):
    if position_idx == 1:  # 롱
        tp = entry_price * (1 + (tp_pnl + commission) / lev)
        sl = entry_price * (1 + (sl_pnl - commission) / lev)
    else:  # 숏
        tp = entry_price * (1 - (tp_pnl - commission) / lev)
        sl = entry_price * (1 - (sl_pnl + commission) / lev)
    return tp, sl

def set_trading_stop(symbol, position_idx, tp_price, sl_price):
    body = {
        "category": "linear",
        "symbol": symbol,
        "positionIdx": position_idx,
    }
    if tp_price:
        body["takeProfit"] = str(tp_price)
    if sl_price:
        body["stopLoss"] = str(sl_price)
    resp = http_request("POST", "/v5/position/trading-stop", body)
    print(f"[TRADING-STOP] set TP/SL: {body}", flush=True)
    print("[TRADING-STOP 응답]", resp.text, flush=True)
    return resp

def clear_trading_stop(symbol, position_idx):
    body = {
        "category": "linear",
        "symbol": symbol,
        "positionIdx": position_idx,
        "takeProfit": "",
        "stopLoss": "",
    }
    resp = http_request("POST", "/v5/position/trading-stop", body)
    print(f"[트레이딩스톱 해제]:", resp.text, flush=True)
    return resp

def get_open_orders(symbol):
    endpoint = '/v5/order/realtime'
    params = {
        'category': 'linear',
        'symbol': symbol,
    }
    resp = http_request('GET', endpoint, params)
    try:
        js = resp.json()
        print(f"[오픈오더 응답] {js}", flush=True)
        if js.get('retCode') == 0:
            return js['result']['list']
    except Exception as e:
        print("[오픈오더 조회 오류]", e, flush=True)
    return []

def cancel_order(symbol, order_id):
    endpoint = '/v5/order/cancel'
    body = {
        'category': 'linear',
        'symbol': symbol,
        'orderId': order_id,
    }
    resp = http_request('POST', endpoint, body)
    print(f"[지정가 오더 취소] {order_id} 결과:", resp.text, flush=True)
    return resp

def place_tp_sl_orders(symbol, qty, tp_price, sl_price, position_idx, entry_price, tick, tp_order_id, sl_order_id):
    side = 'Sell' if position_idx == 1 else 'Buy'
    tp_price_rounded = round_to_tick(tp_price, tick)
    sl_price_rounded = round_to_tick(sl_price, tick)
    qty_str = get_qty_str(symbol, qty)
    print(f"[TP/SL 주문발행] side: {side}, qty: {qty_str}, TP: {tp_price_rounded}, SL: {sl_price_rounded}", flush=True)
    tp_body = {
        'category': 'linear',
        'symbol': symbol,
        'side': side,
        'orderType': 'Limit',
        'qty': qty_str,
        'price': str(tp_price_rounded),
        'timeInForce': 'GoodTillCancel',
        'reduceOnly': True,
        'orderLinkId': tp_order_id,
    }
    tp_resp = http_request('POST', '/v5/order/create', tp_body)
    print(">>> [TP 주문 요청 결과] ", tp_resp.text, flush=True)

    sl_body = {
        'category': 'linear',
        'symbol': symbol,
        'side': side,
        'orderType': 'Limit',
        'qty': qty_str,
        'price': str(sl_price_rounded),
        'timeInForce': 'GoodTillCancel',
        'reduceOnly': True,
        'orderLinkId': sl_order_id,
    }
    sl_resp = http_request('POST', '/v5/order/create', sl_body)
    print(">>> [SL 주문 요청 결과] ", sl_resp.text, flush=True)

def place_dual_tp_sl(symbol, qty, tp_price, sl_price, position_idx, entry_price, tick, tp_order_id, sl_order_id):
    place_tp_sl_orders(symbol, qty, tp_price, sl_price, position_idx, entry_price, tick, tp_order_id, sl_order_id)
    set_trading_stop(symbol, position_idx, tp_price, sl_price)

def monitor_and_cleanup(symbol, position_idx, tp_order_id, sl_order_id):
    print("[모니터링] 지정가 TP/SL 청산시 자동정리 시작", flush=True)
    while True:
        size = get_position_size(symbol, position_idx)
        if size == 0:
            open_orders = get_open_orders(symbol)
            for order in open_orders:
                if order.get('orderLinkId') in [tp_order_id, sl_order_id]:
                    cancel_order(symbol, order['orderId'])
            clear_trading_stop(symbol, position_idx)
            print("[모니터링] 청산 감지 후, 잔여 오더 및 트레이딩스톱 해제 완료", flush=True)
            break
        time.sleep(1)

def monitor_trailing_stop(symbol, position_idx, entry_price, lev, policy):
    steps = policy.get('trailing_steps', None)
    commission = COMMISSION
    if not steps:
        return

    step_idx = 0
    triggered = set()
    while True:
        size = get_position_size(symbol, position_idx)
        if size == 0:
            break

        endpoint = '/v5/position/list'
        body = {"category": "linear", "symbol": symbol}
        resp = http_request('GET', endpoint, body)
        try:
            data = resp.json()
            if data.get('retCode') == 0:
                pos_list = data['result']['list']
                for pos in pos_list:
                    if int(pos.get('positionIdx', 0)) == position_idx:
                        entry = float(pos.get('avgPrice', entry_price))
                        last_price = float(pos.get('markPrice', entry))
                        direction = 1 if position_idx == 1 else -1
                        pnl_rate = direction * (last_price - entry) / entry * lev

                        for i, s in enumerate(steps):
                            trigger = s['trigger']
                            trail_sl = s['sl']
                            if i not in triggered and pnl_rate >= trigger:
                                if position_idx == 1:  # 롱
                                    new_sl = entry * (1 + (trail_sl - commission) / lev)
                                else:
                                    new_sl = entry * (1 - (trail_sl + commission) / lev)
                                set_trading_stop(symbol, position_idx, "", new_sl)
                                print(f"[트레일링스탑 {i+1}회차 적용] {symbol} SL → {trail_sl*100:.2f}% (실행가: {new_sl})", flush=True)
                                triggered.add(i)
        except Exception:
            print("[트레일링스탑 오류]", flush=True)
        time.sleep(1)

def place_order(signal, symbol, req_json):
    try:
        bybit_symbol = get_underlying_symbol(symbol)
        qty = get_order_qty(bybit_symbol, order_type="Market")
        if qty is None or qty == 0:
            print("[ERROR] 주문수량 0, 진입 스킵", flush=True)
            return {'error': 'Order qty 0, skip'}
        qty_str = get_qty_str(bybit_symbol, qty)
        client_order_id = f"entry_{uuid.uuid4().hex}"
        qty_for_api = qty_str

        policy = get_symbol_policy(bybit_symbol)
        tp_pnl = policy['tp']
        sl_pnl = policy['sl']
        trailing_steps = policy.get('trailing_steps')

        if signal == 'buy':
            set_leverage_and_mode(bybit_symbol, TRADE_LEVERAGE, TRADE_LEVERAGE, TRADE_MARGIN_MODE)
            if has_open_position(bybit_symbol, 2):
                closed = close_position_and_wait(bybit_symbol, 'Sell')
                if not closed:
                    print("[ERROR] 숏 청산 지연", flush=True)
                    return {'error': '숏 청산 지연'}
            if not has_open_position(bybit_symbol, 1):
                endpoint = '/v5/order/create'
                body = {
                    'category': 'linear',
                    'symbol': bybit_symbol,
                    'side': 'Buy',
                    'orderType': 'Market',
                    'qty': qty_for_api,
                    'positionIdx': 1,
                    'orderLinkId': client_order_id
                }
                print("[LONG 주문 요청]", body, flush=True)
                resp = http_request('POST', endpoint, body)
                print("[LONG 주문 응답]", resp.text, flush=True)
                try:
                    r_json = resp.json()
                    if r_json.get('retCode') != 0:
                        print("[LONG 주문 Bybit API Error]:", r_json, flush=True)
                except Exception as e:
                    print("[LONG 주문 Bybit API JSON decode error]:", resp.text, flush=True)

                actual_size = wait_until_position_open(bybit_symbol, 1, timeout=10, interval=0.5)
                if actual_size == 0:
                    print("[경고] 진입 후 10초 내 포지션 생성 안됨!", flush=True)
                    return {'error': '포지션 생성 실패'}
                entry_price = get_position_entry_price(bybit_symbol, 1)
                if entry_price is None or entry_price < 0.00001:
                    price_endpoint = '/v5/market/tickers'
                    price_body = {'category': 'linear', 'symbol': bybit_symbol}
                    price_resp = http_request('GET', price_endpoint, price_body)
                    try:
                        js = price_resp.json()
                        entry_price = float(js['result']['list'][0]['lastPrice'])
                        print(f"[TP/SL] 체결가/포지션가 없음 → 현재가({entry_price})로 TP/SL 생성", flush=True)
                    except Exception:
                        print("[TP/SL] 현재가 조회 실패", flush=True)
                        entry_price = None
                if entry_price is None or entry_price < 0.00001:
                    print("[경고] entry_price 값이 비정상입니다:", entry_price, flush=True)
                    return {'error': '진입가 조회 실패'}

                tick = SYMBOL_TICK_SIZE.get(bybit_symbol, 0.01)
                tp_price, sl_price = get_tp_sl_by_real_pnl(
                    entry_price, 1, TRADE_LEVERAGE,
                    tp_pnl=tp_pnl, sl_pnl=sl_pnl, commission=COMMISSION
                )
                if trailing_steps:
                    threading.Thread(
                        target=monitor_trailing_stop,
                        args=(bybit_symbol, 1, entry_price, TRADE_LEVERAGE, policy),
                        daemon=True
                    ).start()
                tp_order_id = f"tp_{uuid.uuid4().hex}"
                sl_order_id = f"sl_{uuid.uuid4().hex}"
                print(f"[DEBUG][LONG] 진입가: {entry_price}, TP: {tp_price}, SL: {sl_price}, tick: {tick}", flush=True)
                place_dual_tp_sl(bybit_symbol, actual_size, tp_price, sl_price, 1, entry_price, tick, tp_order_id, sl_order_id)
                threading.Thread(target=monitor_and_cleanup, args=(bybit_symbol, 1, tp_order_id, sl_order_id), daemon=True).start()

        elif signal == 'sell':
            set_leverage_and_mode(bybit_symbol, TRADE_LEVERAGE, TRADE_LEVERAGE, TRADE_MARGIN_MODE)
            if has_open_position(bybit_symbol, 1):
                closed = close_position_and_wait(bybit_symbol, 'Buy')
                if not closed:
                    print("[ERROR] 롱 청산 지연", flush=True)
                    return {'error': '롱 청산 지연'}
            if not has_open_position(bybit_symbol, 2):
                endpoint = '/v5/order/create'
                body = {
                    'category': 'linear',
                    'symbol': bybit_symbol,
                    'side': 'Sell',
                    'orderType': 'Market',
                    'qty': qty_for_api,
                    'positionIdx': 2,
                    'orderLinkId': client_order_id
                }
                print("[SHORT 주문 요청]", body, flush=True)
                resp = http_request('POST', endpoint, body)
                print("[SHORT 주문 응답]", resp.text, flush=True)
                try:
                    r_json = resp.json()
                    if r_json.get('retCode') != 0:
                        print("[SHORT 주문 Bybit API Error]:", r_json, flush=True)
                except Exception as e:
                    print("[SHORT 주문 Bybit API JSON decode error]:", resp.text, flush=True)

                actual_size = wait_until_position_open(bybit_symbol, 2, timeout=10, interval=0.5)
                if actual_size == 0:
                    print("[경고] 진입 후 10초 내 포지션 생성 안됨!", flush=True)
                    return {'error': '포지션 생성 실패'}
                entry_price = get_position_entry_price(bybit_symbol, 2)
                if entry_price is None or entry_price < 0.00001:
                    price_endpoint = '/v5/market/tickers'
                    price_body = {'category': 'linear', 'symbol': bybit_symbol}
                    price_resp = http_request('GET', price_endpoint, price_body)
                    try:
                        js = price_resp.json()
                        entry_price = float(js['result']['list'][0]['lastPrice'])
                        print(f"[TP/SL] 체결가/포지션가 없음 → 현재가({entry_price})로 TP/SL 생성", flush=True)
                    except Exception:
                        print("[TP/SL] 현재가 조회 실패", flush=True)
                        entry_price = None
                if entry_price is None or entry_price < 0.00001:
                    print("[경고] entry_price 값이 비정상입니다:", entry_price, flush=True)
                    return {'error': '진입가 조회 실패'}

                tick = SYMBOL_TICK_SIZE.get(bybit_symbol, 0.01)
                tp_price, sl_price = get_tp_sl_by_real_pnl(
                    entry_price, 2, TRADE_LEVERAGE,
                    tp_pnl=tp_pnl, sl_pnl=sl_pnl, commission=COMMISSION
                )
                if trailing_steps:
                    threading.Thread(
                        target=monitor_trailing_stop,
                        args=(bybit_symbol, 2, entry_price, TRADE_LEVERAGE, policy),
                        daemon=True
                    ).start()
                tp_order_id = f"tp_{uuid.uuid4().hex}"
                sl_order_id = f"sl_{uuid.uuid4().hex}"
                print(f"[DEBUG][SHORT] 진입가: {entry_price}, TP: {tp_price}, SL: {sl_price}, tick: {tick}", flush=True)
                place_dual_tp_sl(bybit_symbol, actual_size, tp_price, sl_price, 2, entry_price, tick, tp_order_id, sl_order_id)
                threading.Thread(target=monitor_and_cleanup, args=(bybit_symbol, 2, tp_order_id, sl_order_id), daemon=True).start()
        else:
            print("[ERROR] Invalid signal", flush=True)
            return {'error': 'Invalid signal'}, 400
        return {'message': f'{signal} processed'}
    except Exception as e:
        print("[place_order ERROR]", traceback.format_exc(), flush=True)
        return {'error': str(e)}

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        raw = request.data.decode('utf-8').strip()
        print(f"[WEBHOOK 수신 RAW]: {raw}", flush=True)
        if not raw:
            print("No payload received from TradingView", flush=True)
            return jsonify({'error': 'No payload received from TradingView'}), 400
        try:
            data = json.loads(raw)
        except Exception as e:
            print("Failed to decode JSON:", e, flush=True)
            print(traceback.format_exc(), flush=True)
            return jsonify({'error': f'Failed to decode JSON: {e}'}), 400
        signal = data.get('signal')
        symbol = data.get('symbol', None)
        if not signal or not symbol:
            print("Invalid signal or symbol", flush=True)
            return jsonify({'error': 'Invalid signal or symbol'}), 400
        print(f"[WEBHOOK] signal:{signal}, symbol:{symbol}, data:{data}", flush=True)
        result = place_order(signal, symbol, data)
        status_code = 200 if 'message' in result else 500
        print("place_order result:", result, flush=True)
        return jsonify(result), status_code
    except Exception as e:
        print(traceback.format_exc(), flush=True)
        return jsonify({'error': str(e)}), 500

@app.route('/')
def home():
    return 'Bybit Flask Multi-Symbol Trading Bot is running!'

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)
