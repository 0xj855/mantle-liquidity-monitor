from datetime import date, datetime, timedelta
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

from .chain import multicall

UA = {"user-agent": "fluxmon/0.1", "accept": "application/json"}
XSTOCKS_API = "https://api.backed.fi/api/v1/token"
RFQ_URL = "https://fluxion-proxy-api-production.up.railway.app/quote"   # 限速 60 次/分钟
KLINE_URL = "https://fluxion.network/api/xstocks/kline"
USPX_KLINE_URL = "https://fluxion.network/api/xstocks/uspx-kline"
YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{}?interval=1m&range=1d&includePrePost=true"
JUP_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
SOLANA_RPC_URL = "https://api.mainnet-beta.solana.com"
SOL_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOL_XSTOCK_DECIMALS = 8
_catalog_payload = None
BREAK_AFTER = 3     # 同一主机在一轮内连续超时 / 5xx 这么多次后熔断，本轮不再请求
_host_fails = {}


def http(url, body=None, timeout=8):
    """返回 (status, 解析后的 JSON 或 None)；status 0 表示超时、连接失败或已熔断。"""
    host = urllib.parse.urlsplit(url).netloc
    if _host_fails.get(host, 0) >= BREAK_AFTER:
        return 0, None
    headers = dict(UA)
    data = None
    if body is not None:
        headers["content-type"] = "application/json"
        data = json.dumps(body).encode()
    try:
        resp = urllib.request.urlopen(urllib.request.Request(url, data, headers), timeout=timeout)
        raw = resp.read()
        _host_fails[host] = 0
        return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:   # 4xx 是正常业务结果（如无报价），只有 5xx 计入熔断
        if e.code >= 500:
            _fail(host)
        else:
            _host_fails[host] = 0
        return e.code, None
    except Exception:
        _fail(host)
        return 0, None


def _fail(host):
    _host_fails[host] = _host_fails.get(host, 0) + 1
    if _host_fails[host] == BREAK_AFTER:
        print(f"http: {host} failed {BREAK_AFTER} times in a row, skipping it for the rest of this run")


def xstocks_catalog():
    """Mantle 上的 xStocks：{address(lower): {symbol, native, kind: native|wrapper}}"""
    global _catalog_payload
    status, payload = http(XSTOCKS_API, timeout=30)
    if status != 200 or not isinstance(payload, dict):
        raise RuntimeError(f"xStocks catalog unavailable: HTTP {status}")
    _catalog_payload = payload
    out = {}
    for a in payload.get("nodes", []):
        sym = a.get("symbol") or ""
        if not sym.lower().endswith("x"):
            continue
        for d in a.get("deployments") or []:
            if d.get("network") != "Mantle" or not d.get("address"):
                continue
            native = d["address"].lower()
            out[native] = {"symbol": sym, "native": native, "kind": "native"}
            for k in ("wrapperAddress", "wrapperAddressV2"):
                if d.get(k):
                    out[d[k].lower()] = {"symbol": sym, "native": native, "kind": "wrapper"}
    return out


def xstocks_solana():
    """Solana 上的 xStocks：{symbol: {mint, underlying}}（复用 xstocks_catalog 拉到的清单）。"""
    payload = _catalog_payload
    if payload is None:
        _, payload = http(XSTOCKS_API, timeout=30)
    out = {}
    for a in (payload or {}).get("nodes", []):
        for d in a.get("deployments") or []:
            if d.get("network") == "Solana" and d.get("address"):
                out[a["symbol"]] = {"mint": d["address"].removeprefix("svm:"), "underlying": a.get("underlyingSymbol")}
    return out


def underlying_price(ticker):
    """美股标的最新成交价（Yahoo 1 分钟线，含盘前盘后）：{price, ts}；失败返回 None。"""
    s, j = http(YAHOO_CHART_URL.format(ticker))
    try:
        r = j["chart"]["result"][0]
        pts = [(t, c) for t, c in zip(r.get("timestamp") or [], r["indicators"]["quote"][0].get("close") or []) if c]
        if pts:
            return {"price": pts[-1][1], "ts": pts[-1][0]}
        return {"price": r["meta"]["regularMarketPrice"], "ts": r["meta"].get("regularMarketTime")}
    except Exception:
        return None


def solana_multiplier(mint, now=None):
    """Token-2022 Scaled UI Amount 的当前 multiplier（1 原始单位 = multiplier 股，用于股息再投资）。"""
    body = {"jsonrpc": "2.0", "id": 1, "method": "getAccountInfo", "params": [mint, {"encoding": "jsonParsed"}]}
    s, j = http(SOLANA_RPC_URL, body)
    try:
        exts = j["result"]["value"]["data"]["parsed"]["info"].get("extensions") or []
    except Exception:
        return None
    for e in exts:
        if e.get("extension") == "scaledUiAmountConfig":
            st = e["state"]
            if (now or time.time()) >= int(st.get("newMultiplierEffectiveTimestamp") or 0):
                return float(st["newMultiplier"])
            return float(st["multiplier"])
    return 1.0


def jupiter_quote(input_mint, output_mint, amount_raw, slippage_bps=100):
    """Jupiter 聚合器 exact-in 报价：{out, route}；无路由返回 None。"""
    q = urllib.parse.urlencode({"inputMint": input_mint, "outputMint": output_mint, "amount": int(amount_raw), "slippageBps": slippage_bps})
    s, j = http(f"{JUP_QUOTE_URL}?{q}")
    if s != 200 or not isinstance(j, dict) or not j.get("outAmount"):
        return None
    return {"out": int(j["outAmount"]), "route": "+".join(r["swapInfo"]["label"] for r in j.get("routePlan", []))}


def eth_usd():
    """ETH 参考价：Binance 与 Coinbase 的中位/均值；全部失败返回 None。"""
    px = []
    s, j = http("https://api.binance.com/api/v3/ticker/price?symbol=ETHUSDT")
    if s == 200 and j:
        px.append(float(j["price"]))
    s, j = http("https://api.coinbase.com/v2/prices/ETH-USD/spot")
    if s == 200 and j:
        px.append(float(j["data"]["amount"]))
    return {"price": sum(px) / len(px), "sources": len(px)} if px else None


def wrapper_ratios(wrappers):
    """ERC-4626 包装币：1 share 对应多少 native xStock（convertToAssets(1e18)/1e18）。"""
    r = multicall([(w, "0x07a2d13a" + format(10 ** 18, "064x")) for w in wrappers])
    return {w: (int(h, 16) / 1e18 if h else None) for w, h in zip(wrappers, r)}


def xstock_reference(symbol, native, usdc, rfq_size_usdc=1000):
    """xStock 参考价：优先 RFQ rawPrice（可成交的市场参考价），否则用 Fluxion 1 分钟 K 线收盘价。"""
    ref = {"symbol": symbol, "rfq_http": None, "rfq_price": None, "kline_price": None, "kline_age_s": None}
    s, j = http(RFQ_URL, {"tokenIn": usdc, "tokenOut": native, "amount": str(rfq_size_usdc * 10 ** 6), "type": "EXACT_INPUT"})
    ref["rfq_http"] = s
    if s == 200 and j and j.get("rawPrice"):
        ref["rfq_price"] = float(j["rawPrice"])
        ref["rfq_raw"] = j
    now = int(time.time() * 1000)
    code = symbol[:-1].replace(".", "").upper()
    url = (f"{USPX_KLINE_URL}?ticker={code}" if symbol == "USPXx" else f"{KLINE_URL}?symbol={code}USDT") \
        + f"&interval=1&from={now - 4 * 86400_000}&to={now}&limit=500"
    s, j = http(url)
    if s == 200 and isinstance(j, list) and j:
        last = max(j, key=lambda k: int(k["startTime"]))
        ref["kline_price"] = float(last["closePrice"])
        ref["kline_age_s"] = (now - int(last["startTime"])) / 1000
    ref["price"] = ref["rfq_price"] or ref["kline_price"]
    ref["source"] = "rfq" if ref["rfq_price"] else ("kline" if ref["kline_price"] else None)
    return ref


def probe_rfq_depth(symbol, native, usdc, depth_sizes_usd=(1000, 10000, 50000, 100000),
                    sell_sizes_usd=(1000, 10000), delay_s=1.05, base_ref=None):
    """探测指定 xStock 的 RFQ 双向做市报价与各档承接深度。"""
    # 1. 基础买入报价（$1,000）
    if base_ref and "rfq_http" in base_ref:
        s1 = base_ref["rfq_http"]
        j1 = base_ref.get("rfq_raw")
    else:
        s1, j1 = http(RFQ_URL, {"tokenIn": usdc, "tokenOut": native, "amount": str(1000 * 10 ** 6), "type": "EXACT_INPUT"})

    if s1 != 200 or not j1 or not j1.get("price"):
        return {
            "symbol": symbol, "native": native, "status": "no_quote", "http_status": s1,
            "ask_1k": None, "bid_1k": None, "mid_price": None,
            "spread_usd": None, "spread_pct": None, "spread_bps": None,
            "buy_depth": {}, "sell_depth": {}, "max_buy_usd": 0, "max_sell_usd": 0
        }

    ask_1k = float(j1["price"])
    raw_1k = float(j1.get("rawPrice", ask_1k))
    buy_depth = {"1000": {"price": ask_1k, "raw_price": raw_1k, "impact_pct": 0.0, "status": 200}}

    # 2. 探测更高买入档位
    for sz in depth_sizes_usd:
        if sz == 1000:
            continue
        time.sleep(delay_s)
        s, j = http(RFQ_URL, {"tokenIn": usdc, "tokenOut": native, "amount": str(sz * 10 ** 6), "type": "EXACT_INPUT"})
        if s == 200 and j and j.get("price"):
            p = float(j["price"])
            rp = float(j.get("rawPrice", p))
            buy_depth[str(sz)] = {"price": p, "raw_price": rp, "impact_pct": round((p / ask_1k - 1) * 100, 4), "status": 200}
        else:
            buy_depth[str(sz)] = {"price": None, "status": s}
            break

    # 3. 探测卖出档位（按 ask_1k 折算股数）
    sell_depth = {}
    base_bid = None
    for sz in sell_sizes_usd:
        shares = sz / ask_1k
        amt_in = int(shares * 10 ** 18)
        time.sleep(delay_s)
        s, j = http(RFQ_URL, {"tokenIn": native, "tokenOut": usdc, "amount": str(amt_in), "type": "EXACT_INPUT"})
        if s == 200 and j and j.get("amountOut"):
            usdc_out = int(j["amountOut"]) / 1e6
            eff_price = usdc_out / shares
            p = float(j.get("price", eff_price))
            rp = float(j.get("rawPrice", eff_price))
            if base_bid is None:
                base_bid = eff_price
            impact = round((1 - eff_price / base_bid) * 100, 4) if base_bid else 0.0
            sell_depth[str(sz)] = {"price": eff_price, "quote_price": p, "raw_price": rp, "impact_pct": impact, "status": 200}
        else:
            sell_depth[str(sz)] = {"price": None, "status": s}
            break

    bid_1k = sell_depth.get("1000", {}).get("price")
    mid = (ask_1k + (bid_1k or ask_1k)) / 2
    spread_usd = round(ask_1k - bid_1k, 4) if bid_1k else None
    spread_pct = round(((ask_1k - bid_1k) / mid * 100), 4) if bid_1k else None
    spread_bps = round(spread_pct * 100, 1) if spread_pct is not None else None

    max_buy = max([int(k) for k, v in buy_depth.items() if v.get("status") == 200] or [0])
    max_sell = max([int(k) for k, v in sell_depth.items() if v.get("status") == 200] or [0])

    return {
        "symbol": symbol, "native": native, "status": "active", "http_status": 200,
        "ask_1k": ask_1k, "bid_1k": bid_1k, "mid_price": mid,
        "spread_usd": spread_usd, "spread_pct": spread_pct, "spread_bps": spread_bps,
        "buy_depth": buy_depth, "sell_depth": sell_depth,
        "max_buy_usd": max_buy, "max_sell_usd": max_sell
    }


def _nth_weekday(y, m, wd, n):
    """y 年 m 月第 n 个星期 wd（0=周一）；n=-1 为最后一个。"""
    if n > 0:
        d = date(y, m, 1)
        return d + timedelta(days=(wd - d.weekday()) % 7 + 7 * (n - 1))
    d = date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1)
    return d - timedelta(days=(d.weekday() - wd) % 7)


def _easter(y):
    a, b, c = y % 19, y // 100, y % 100
    d, e = b // 4, b % 4
    g = (8 * b + 13) // 25
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    return date(y, (h + l - 7 * m + 114) // 31, (h + l - 7 * m + 114) % 31 + 1)


def us_market_calendar(y):
    """NYSE 全天休市日与 13:00 提前收盘日（按交易所规则推算，不含临时休市）。"""
    def observed(d):   # 周六 → 周五，周日 → 周一
        return d - timedelta(days=1) if d.weekday() == 5 else d + timedelta(days=1) if d.weekday() == 6 else d
    nyd = date(y, 1, 1)
    holidays = {nyd + timedelta(days=1)} if nyd.weekday() == 6 else ({nyd} if nyd.weekday() < 5 else set())   # 元旦逢周六不补休
    holidays |= {_nth_weekday(y, 1, 0, 3), _nth_weekday(y, 2, 0, 3), _easter(y) - timedelta(days=2),
                 _nth_weekday(y, 5, 0, -1), observed(date(y, 6, 19)), observed(date(y, 7, 4)),
                 _nth_weekday(y, 9, 0, 1), _nth_weekday(y, 11, 3, 4), observed(date(y, 12, 25))}
    early = {_nth_weekday(y, 11, 3, 4) + timedelta(days=1)}
    for d in (date(y, 7, 3), date(y, 12, 24)):
        if d.weekday() < 5 and d not in holidays:
            early.add(d)
    return holidays, early


def get_trading_session(dt=None):
    """根据美东时间（ET）计算当前美股交易时段：
    - market_open: 9:30 AM – 4:00 PM ET (周一至周五)
    - pre_post: 盘前盘后工作日时段 (周一至周五其余时间)
    - weekend: 周末时段 (周六、周日)
    """
    if dt is None:
        dt = datetime.now(ZoneInfo("America/New_York"))
    elif dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(ZoneInfo("America/New_York"))
    else:
        dt = dt.astimezone(ZoneInfo("America/New_York"))

    weekday = dt.weekday()
    mins = dt.hour * 60 + dt.minute

    holidays, early = us_market_calendar(dt.year)
    close = 13 * 60 if dt.date() in early else 16 * 60

    if weekday in (5, 6) or dt.date() in holidays:   # 休市日沿用周末的 SLA 基准
        session = "weekend"
        name_zh = "周末时段 (Weekend Hours)" if weekday in (5, 6) else "美股休市日 (Market Holiday)"
        name_en = "Weekend Hours" if weekday in (5, 6) else "Market Holiday"
        is_open = False
    elif 9 * 60 + 30 <= mins < close:
        session = "market_open"
        name_zh = "美股盘中时段 (Market Open, 9:30 AM – 4:00 PM ET)"
        name_en = "Market Open Hours (9:30 AM – 4:00 PM ET)"
        is_open = True
    else:
        session = "pre_post"
        is_pre = mins < 9 * 60 + 30
        tag = "盘前" if is_pre else "盘后"
        name_zh = f"工作日{tag}时段 (Pre-Post Weekday Hours)"
        name_en = f"Pre-Post Weekday Hours ({'Pre-Market' if is_pre else 'Post-Market'})"
        is_open = False

    return {
        "session": session,
        "name_zh": name_zh,
        "name_en": name_en,
        "et_time": dt.strftime("%H:%M:%S ET"),
        "is_open": is_open
    }


