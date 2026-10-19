"""Fluxion RFQ 的 SLA 执行记录：每轮每个标的一条明细，长期保存为月度 CSV，并汇总成看板用的 sla.json。

存储分两层：
- SQLite 表 rfq_sla 只做 7 天缓冲；
- 月度 CSV（<dir>/rfq/YYYY-MM.csv，UTC 月份）是长期记录，CI 中位于 sla-data 分支，每次只追加比文件里更新的样本。
汇总口径：按 UTC 日期；达标率 = 达标样本 / 考核样本，考核样本 = 全部样本 − 非盘中时段无报价的样本
（盘中缺报价算不达标）；另给报价在线率与报价时达标率。
"""
import csv
import json
import statistics
import time
from pathlib import Path

FIELDS = ["ts", "time_utc", "symbol", "tier", "session", "holiday", "quoted", "ask_1k", "bid_1k", "mid_price",
          "spread_bps", "mid_bps", "bm_lo_bps", "bm_hi_bps", "sla_status", "max_buy_usd", "max_sell_usd",
          "impact_buy_10k_pct", "impact_buy_50k_pct", "impact_buy_100k_pct"]
STATUSES = ["pass", "wide", "no_bid", "missing_open", "unquoted_off_hours"]
BUFFER_DAYS = 7
SUMMARY_VERSION = 2


def _ensure(store):
    store.db.execute("CREATE TABLE IF NOT EXISTS rfq_sla (ts INT, symbol TEXT, data TEXT, PRIMARY KEY (ts, symbol))")


def record(store, items, session, now):
    """items: probe_rfq_depth 结果（已带 tier / sla_* 字段）；session: get_trading_session()。"""
    _ensure(store)
    holiday = 1 if session.get("name_en") == "Market Holiday" else 0
    rows = []
    for it in items:
        bd = it.get("buy_depth") or {}
        bm = it.get("sla_bm_mid_bps") or [None, None]
        imp = lambda k: (bd.get(k) or {}).get("impact_pct")
        rows.append((now, it["symbol"], json.dumps([
            now, time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)), it["symbol"], it.get("tier"), session["session"], holiday,
            1 if it.get("status") == "active" else 0, it.get("ask_1k"), it.get("bid_1k"), it.get("mid_price"),
            it.get("spread_bps"), it.get("actual_mid_bps"), bm[0], bm[1], it.get("sla_status"),
            it.get("max_buy_usd"), it.get("max_sell_usd"), imp("10000"), imp("50000"), imp("100000")])))
    store.db.executemany("INSERT OR REPLACE INTO rfq_sla VALUES (?,?,?)", rows)
    store.db.execute("DELETE FROM rfq_sla WHERE ts<?", (now - BUFFER_DAYS * 86400,))
    store.db.commit()


def _last_ts(path):
    if not path.exists():
        return 0
    last = 0
    with path.open() as f:
        for row in csv.reader(f):
            if row and row[0].isdigit():
                last = max(last, int(row[0]))
    return last


def export(store, out_dir):
    """把缓冲里比 CSV 更新的样本追加到对应月份文件。返回追加行数。"""
    _ensure(store)
    rfq = Path(out_dir) / "rfq"
    rfq.mkdir(parents=True, exist_ok=True)
    by_month = {}
    for ts, data in store.db.execute("SELECT ts, data FROM rfq_sla ORDER BY ts, symbol"):
        by_month.setdefault(time.strftime("%Y-%m", time.gmtime(ts)), []).append(json.loads(data))
    n = 0
    for month, rows in by_month.items():
        path = rfq / f"{month}.csv"
        last = _last_ts(path)
        new = [r for r in rows if r[0] > last]
        if not new:
            continue
        fresh = not path.exists()
        with path.open("a", newline="") as f:
            w = csv.writer(f)
            if fresh:
                w.writerow(FIELDS)
            w.writerows(["" if v is None else v for v in r] for r in new)
        n += len(new)
    return n


def _num(v):
    try:
        return float(v) if v not in ("", None) else None
    except ValueError:
        return None


def _agg(rows):
    """一组样本 -> 计数、点差与深度的分位数。"""
    out = {"n": len(rows), "quoted": sum(r["quoted"] for r in rows)}
    for s in STATUSES:
        out[s] = sum(1 for r in rows if r["sla_status"] == s)
    mids = sorted(r["mid_bps"] for r in rows if r["mid_bps"] is not None)
    depth = [r["max_buy_usd"] for r in rows if r["quoted"] and r["max_buy_usd"] is not None]
    q = lambda xs, p: xs[min(len(xs) - 1, int(p * (len(xs) - 1) + 0.5))] if xs else None
    out["eligible"] = out["n"] - out["unquoted_off_hours"]   # 达标率分母：非盘中无报价不考核
    bms = [r["bm_hi"] for r in rows if r["bm_hi"] is not None]   # rows 按时间排序，取期间内最后生效的基准
    out.update(mid_med=q(mids, .5), mid_p90=q(mids, .9), depth_med=statistics.median(depth) if depth else None,
               bm_hi=bms[-1] if bms else None)
    return out


def _load_month(path):
    rows = []
    with path.open() as f:
        for r in csv.DictReader(f):
            rows.append({"ts": int(r["ts"]), "day": r["time_utc"][:10], "symbol": r["symbol"], "tier": r["tier"],
                         "session": r["session"], "quoted": int(r["quoted"] or 0), "sla_status": r["sla_status"],
                         "mid_bps": _num(r["mid_bps"]), "bm_hi": _num(r["bm_hi_bps"]), "max_buy_usd": _num(r["max_buy_usd"])})
    return rows


def _group(rows):
    """{symbol: {session|"all": agg}}"""
    g = {}
    for r in rows:
        for sess in (r["session"], "all"):
            g.setdefault(r["symbol"], {}).setdefault(sess, []).append(r)
    return {sym: {s: _agg(v) for s, v in d.items()} for sym, d in g.items()}


def summarize(out_dir, now):
    """读全部月度 CSV，输出 {periods, daily, months, tiers, first_ts}。已结束的月份汇总缓存在 <dir>/summary/。"""
    rfq, cache = Path(out_dir) / "rfq", Path(out_dir) / "summary"
    cache.mkdir(parents=True, exist_ok=True)
    this_month = time.strftime("%Y-%m", time.gmtime(now))
    months, daily, recent, tiers, first = {}, {}, [], {}, None
    for path in sorted(rfq.glob("*.csv")):
        m = path.stem
        cached = cache / f"{m}.json"
        if m != this_month and cached.exists():
            c = json.loads(cached.read_text())
            if c.get("v") == SUMMARY_VERSION and c.get("size") == path.stat().st_size:   # CSV 被补写过则重算
                months[m] = c["month"]
                daily.update(c["daily"])
                tiers.update(c["tiers"])
                first = min(first or c["first_ts"], c["first_ts"])
                if now - c["last_ts"] < 31 * 86400:
                    recent += [r for r in _load_month(path) if r["ts"] >= now - 30 * 86400]
                continue
        rows = _load_month(path)
        if not rows:
            continue
        by_day = {}
        for r in rows:
            by_day.setdefault(r["day"], []).append(r)
            tiers[r["symbol"]] = r["tier"]
        md = {d: _group(v) for d, v in by_day.items()}
        months[m] = _group(rows)
        daily.update(md)
        first = min(first or rows[0]["ts"], min(r["ts"] for r in rows))
        recent += [r for r in rows if r["ts"] >= now - 30 * 86400]
        if m != this_month:
            cached.write_text(json.dumps({"v": SUMMARY_VERSION, "size": path.stat().st_size, "month": months[m], "daily": md, "tiers": tiers,
                                          "first_ts": min(r["ts"] for r in rows), "last_ts": max(r["ts"] for r in rows)}))
    periods = {"7d": _group([r for r in recent if r["ts"] >= now - 7 * 86400]), "30d": _group(recent)}
    periods.update({f"m:{m}": v for m, v in months.items()})
    return {"ts": now, "first_ts": first, "tiers": tiers, "periods": periods, "daily": daily,
            "months": sorted(months, reverse=True), "statuses": STATUSES}
