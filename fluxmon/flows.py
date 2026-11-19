"""xStocks 在 Mantle 上的流通与 Bybit 充提：增量同步原生 xStocks 的 Transfer 事件，
按地址标签（config → flows.labels）区分交易所钱包与发行方，统计提现（交易所 → 链上）与充值（链上 → 交易所）。
"""
import time

from . import chain

TRANSFER = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
ZERO = "0x" + "0" * 40
BACKFILL_DAYS = 30
WINDOWS = {"24h": 86400, "7d": 7 * 86400, "30d": 30 * 86400}


def sync(store, natives, head):
    """natives: {symbol: native 地址}。首次回填 30 天，之后只扫新区块；新增标的单独补扫 30 天。"""
    backfill = head - BACKFILL_DAYS * chain.BLOCKS_PER_DAY
    prev_block = store.get("xfers_synced_block")
    synced_syms = set(store.get("xfers_synced_syms") or (
        [r[0] for r in store.db.execute("SELECT DISTINCT symbol FROM xfers")] if prev_block else []
    ))
    rows = []
    for sym, tok in natives.items():
        start = prev_block if (prev_block and sym in synced_syms) else backfill
        if start >= head:
            continue
        for l in chain.get_logs(tok, [TRANSFER], start + 1, head):
            rows.append((int(l["blockNumber"], 16), int(l["logIndex"], 16), sym, "0x" + l["topics"][1][-40:],
                         "0x" + l["topics"][2][-40:], int(l["data"], 16) / 1e18))
    if rows:
        store.add_xfers(rows)
    store.put("xfers_synced_block", head)
    store.put("xfers_synced_syms", sorted(synced_syms | set(natives)))
    return len(rows)


def summarize(store, natives, labels, prices, head, now, keep_days=35):
    """labels: {地址: {"name", "kind": "exchange" | "issuer"}}；prices: {symbol: 美元价}。"""
    lab = {a.lower(): v for a, v in labels.items()}
    ex = {a for a, v in lab.items() if v.get("kind") == "exchange"}
    issuer = {a for a, v in lab.items() if v.get("kind") == "issuer"}
    bpd = chain.BLOCKS_PER_DAY

    # 当前持仓：总供应、发行方库存、交易所持仓
    syms = list(natives)
    calls = [(natives[s], "0x18160ddd") for s in syms]
    holders = sorted(ex | issuer)
    calls += [(natives[s], "0x70a08231" + h[2:].rjust(64, "0")) for s in syms for h in holders]
    r = chain.multicall(calls)
    val = lambda h: int(h, 16) / 1e18 if h else 0.0
    per = {}
    for i, s in enumerate(syms):
        px = prices.get(s) or 0
        bal = {h: val(r[len(syms) + i * len(holders) + j]) for j, h in enumerate(holders)}
        supply = val(r[i])
        iss = sum(bal[h] for h in issuer)
        exch = sum(bal[h] for h in ex)
        per[s] = {"price": px, "supply_usd": supply * px, "issuer_usd": iss * px, "exchange_usd": exch * px,
                  "circulating_usd": (supply - iss) * px, "onchain_usd": (supply - iss - exch) * px}

    # 各窗口的充提与链上转账
    win = {}
    for name, secs in WINDOWS.items():
        since = head - int(secs / 86400 * bpd)
        w = {"wd_n": 0, "wd_usd": 0.0, "wd_addrs": set(), "dp_n": 0, "dp_usd": 0.0, "dp_addrs": set(), "xfer_usd": 0.0, "by_sym": {}}
        for blk, sym, f, t, amt in store.xfers_since(since):
            usd = amt * (prices.get(sym) or 0)
            bs = w["by_sym"].setdefault(sym, {"wd_usd": 0.0, "dp_usd": 0.0})
            if f != ZERO and t != ZERO:
                w["xfer_usd"] += usd
            if f in ex and t not in ex:
                w["wd_n"] += 1; w["wd_usd"] += usd; w["wd_addrs"].add(t); bs["wd_usd"] += usd
            elif t in ex and f not in ex:
                w["dp_n"] += 1; w["dp_usd"] += usd; w["dp_addrs"].add(f); bs["dp_usd"] += usd
        w["wd_addrs"], w["dp_addrs"] = len(w["wd_addrs"]), len(w["dp_addrs"])
        w["net_usd"] = w["wd_usd"] - w["dp_usd"]
        win[name] = w

    # 日序列（看板走势用）
    daily = {}
    for blk, sym, f, t, amt in store.xfers_since(head - 30 * bpd):
        day = time.strftime("%Y-%m-%d", time.gmtime(now - (head - blk) * 86400 / bpd))
        d = daily.setdefault(day, [0.0, 0.0])
        usd = amt * (prices.get(sym) or 0)
        if f in ex and t not in ex:
            d[0] += usd
        elif t in ex and f not in ex:
            d[1] += usd
    store.prune_xfers(head - keep_days * bpd)
    tot = lambda k: sum(v[k] for v in per.values())
    return {"ts": now, "symbols": per, "windows": win, "daily": [[k, *v] for k, v in sorted(daily.items())],
            "totals": {k: tot(k) for k in ("supply_usd", "issuer_usd", "exchange_usd", "circulating_usd", "onchain_usd")},
            "labels": {a: v.get("name") for a, v in lab.items()}}
