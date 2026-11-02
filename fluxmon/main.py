"""入口：python3 -m fluxmon.main [--no-notify] [--skip-positions]"""
import argparse
import json
import os
import time
import urllib.request
from pathlib import Path

from . import chain
from .alerts import evaluate, message_zh
from . import lb
from . import counterparty, flows, mailer, sla, venue
from .analyze import analyze_pool, lp_concentration, lp_from_holders, lp_holders, orient
from .chain import BLOCKS_PER_DAY, decode_string, multicall
from .discover import sweep_pools, sync_events, sync_pools, sync_positions
from .sources import eth_usd, get_trading_session, probe_rfq_depth, wrapper_ratios, xstock_reference, xstocks_catalog
from .store import Store
from .v3 import read_pool

ROOT = Path(__file__).resolve().parent.parent


def clean(o):
    """JSON 不支持 inf/nan，统一转成 null。"""
    if isinstance(o, float):
        return o if o == o and o not in (float("inf"), float("-inf")) else None
    if isinstance(o, dict):
        return {k: clean(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [clean(v) for v in o]
    return o


def token_meta(store, addrs):
    meta = store.get("token_meta", {})
    todo = [a for a in addrs if a not in meta]
    if todo:
        r = multicall([(a, "0x95d89b41") for a in todo] + [(a, "0x313ce567") for a in todo])
        for i, a in enumerate(todo):
            meta[a] = {"symbol": decode_string(r[i]) if r[i] else a[:8], "decimals": int(r[len(todo) + i], 16)}
        store.put("token_meta", meta)
    return meta


def load_config():
    """config.json + 私有覆盖（config/private.json 或环境变量 FLUXMON_PRIVATE_CONFIG）。

    私有部分放 LP 标签、合同承诺金额等不宜进公开仓库的信息。"""
    cfg = json.loads((ROOT / "config" / "config.json").read_text())
    priv = ROOT / "config" / "private.json"
    extra = json.loads(priv.read_text()) if priv.exists() else {}
    if os.environ.get("FLUXMON_PRIVATE_CONFIG"):
        extra = json.loads(os.environ["FLUXMON_PRIVATE_CONFIG"])
    for k, v in extra.items():
        cfg[k] = {**cfg.get(k, {}), **v} if isinstance(v, dict) and isinstance(cfg.get(k), dict) else v
    return cfg


def designated_summary(rows, cfg, labels, xcat):
    """SOW 约定的指定交易对：合计 TVL、按 LP 地址汇总的资金、承诺资金方是否达标。"""
    d = cfg.get("designated")
    if not d:
        return None
    want = set(d["pairs"])
    owners = {o.lower() for o in d.get("committed_owners", [])}
    pools = [r for r in rows if r["dex"] == d.get("dex", "fluxion") and r["base_addr"] in xcat
             and xcat[r["base_addr"]]["symbol"] in want and r["quote"] == "USDC"]
    # 同一交易对若有多个 USDC 池（不同费率 / 包装币版本），全部计入
    holders, per_pool = {}, []
    for r in sorted(pools, key=lambda r: -(r["tvl_usd"] or 0)):
        hs = (r.get("lp") or {}).get("holders", [])
        committed = sum(h["usd"] for h in hs if h["owner"] in owners)
        per_pool.append({"pool": r["pool"], "pair": r["pair"], "symbol": xcat[r["base_addr"]]["symbol"], "tvl_usd": r["tvl_usd"],
                         "committed_usd": committed, "other_usd": sum(h["usd"] for h in hs) - committed,
                         "other_lps": sum(1 for h in hs if h["owner"] not in owners)})
        for h in hs:
            agg = holders.setdefault(h["owner"], {"owner": h["owner"], "label": labels.get(h["owner"]), "usd": 0.0, "pools": 0, "positions": 0})
            agg["usd"] += h["usd"]; agg["pools"] += 1; agg["positions"] += h["positions"]
    tvl = sum(p["tvl_usd"] or 0 for p in per_pool)
    hl = sorted(holders.values(), key=lambda h: -h["usd"])
    for h in hl:
        h["share_pct"] = h["usd"] / tvl * 100 if tvl else None
    committed = sum(h["usd"] for h in hl if h["owner"] in owners)
    covered = sorted({p["symbol"] for p in per_pool if p["committed_usd"] > 0})
    return {"pairs": d["pairs"], "commitment_usd": d.get("commitment_usd"), "committed_owners": sorted(owners),
            "committed_label": d.get("committed_label"), "tvl_usd": tvl, "committed_usd": committed,
            "unattributed_usd": tvl - sum(h["usd"] for h in hl), "missing_pairs": sorted(want - set(covered)) if owners else [],
            "holders": hl, "pools": per_pool}


def designated_alerts(ds, cfg):
    if not ds:
        return []
    out, who = [], ds.get("committed_label") or "committed LP"
    if ds["commitment_usd"] and ds["committed_usd"] < ds["commitment_usd"]:
        out.append(("designated:commitment", "critical", "commitment",
                    {"name": who, "usd": ds["committed_usd"], "min": ds["commitment_usd"]}))
    if ds["missing_pairs"]:
        out.append(("designated:missing_pairs", "critical", "missing_pairs",
                    {"name": who, "pairs": ds["missing_pairs"]}))
    return out


PRIVATE_DESIGNATED = ("commitment_usd", "committed_owners", "committed_label")


def public_view(rep, cfg, xcat):
    """会公开发布的报告（看板数据、latest.md、公开的 Actions 日志）：去掉私有配置带来的 LP 标签、
    合同承诺金额与承诺方，designated 按无私有配置重算；私有告警只走飞书 / 邮件。"""
    pools = [{**r, "lp": {**r["lp"], "holders": [{**h, "label": None} for h in r["lp"].get("holders", [])]}} if r.get("lp") else r
             for r in rep["pools"]]
    pub_cfg = {**cfg, "designated": {k: v for k, v in cfg["designated"].items() if k not in PRIVATE_DESIGNATED}} \
        if cfg.get("designated") else cfg
    return {**rep, "pools": pools, "lp_labels": {}, "designated": designated_summary(pools, pub_cfg, {}, xcat),
            "alerts": [a for a in rep["alerts"] if not a["key"].startswith("designated:")]}


def notify(cfg, lines):
    hook = os.environ.get(cfg["lark_webhook_env"])
    if not hook or not lines:
        return
    link = f"\n看板：{os.environ['FLUXMON_DASHBOARD_URL']}" if os.environ.get("FLUXMON_DASHBOARD_URL") else ""
    body = json.dumps({"msg_type": "text", "content": {"text": "【Mantle 流动性监控】\n" + "\n".join(lines) + link}}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(hook, body, {"content-type": "application/json"}), timeout=15)
    except Exception as e:
        print("lark webhook failed:", e)


def run(args):
    cfg = load_config()
    chain.RPC_URL = cfg["rpc_url"]
    public = bool(os.environ.get("FLUXMON_PUBLIC"))   # 产物会公开发布
    store = Store(ROOT / "data" / "fluxmon.db")
    now = int(time.time())
    t0 = time.time()
    budget = cfg.get("run_budget_s", 420)

    def stage(name, optional=False):
        """打印阶段耗时；optional 阶段在超出整轮时间预算时跳过（返回 False）。"""
        el = time.time() - t0
        if optional and el > budget:
            print(f"[{el:4.0f}s] over budget ({budget}s), skipping {name}")
            return False
        print(f"[{el:4.0f}s] {name}")
        return True
    hist_url = os.environ.get("FLUXMON_HISTORY_URL")
    if hist_url and not store.has_snapshots():   # CI 缓存丢失：从线上看板续接历史
        try:
            hist = json.load(urllib.request.urlopen(urllib.request.Request(hist_url, headers={"user-agent": "fluxmon"}), timeout=30))
            print("imported history rows:", store.import_history(hist))
        except Exception as e:
            print("history import skipped:", e)
    stage("chain head")
    head = chain.block_number()

    xcat = xstocks_catalog()
    weth = cfg["weth"]
    dexes = {d["id"]: d for d in cfg["dexes"]}
    seed = ROOT / "config" / "pools_seed.json"
    if store.get("pools_synced_block") is None and seed.exists():   # 全新环境：用种子清单跳过 Fluxion 从部署区块开始的全量扫描
        sd = json.loads(seed.read_text())
        store.add_pools(sd["pools"])
        store.put("pools_synced_block", sd["synced_block"])
    for d in dexes.values():
        if d["type"] == "lb":
            lb.sync_pairs(store, d)
        elif d["discovery"] == "events":
            sync_pools(store, d, head)
        else:
            quotes = sorted(set(cfg["stables"]) | {weth} | {a.lower() for a in d.get("quotes", [])})
            sweep_pools(store, d, sorted(xcat), quotes, now)
    # 只看两侧都是可信资产（稳定币 / WETH / xStocks / 配置白名单）的池子，排除测试币池；每个 DEX 只看它配置的资产类别
    trusted = set(cfg["stables"]) | {weth} | set(xcat) | {a.lower() for a in cfg.get("extra_tokens", {})}

    def wanted(p):
        assets = dexes.get(p["dex"], {}).get("assets", [])
        hit = ("xstock" in assets and (p["token0"] in xcat or p["token1"] in xcat)) or \
              ("weth" in assets and weth in (p["token0"], p["token1"]))
        return hit and p["token0"] in trusted and p["token1"] in trusted
    pools = [p for p in store.pools() if p["dex"] in dexes and wanted(p)]
    meta = token_meta(store, sorted({t for p in pools for t in (p["token0"], p["token1"])}))
    states = {p["pool"]: (lb.read_pair(p) if p["kind"] == "lb" else read_pool(p)) for p in pools}

    # ---- 美元定价：稳定币=1，WETH=CEX，其余代币用与稳定币/WETH 配对、储备最大的池子推导
    usd = {a: 1.0 for a in cfg["stables"]}
    eth = eth_usd()
    usd[weth] = eth["price"] if eth else None
    for p in sorted(pools, key=lambda p: -(states[p["pool"]]["bal0"] + states[p["pool"]]["bal1"])):
        st = states[p["pool"]]
        for a, b in ((p["token0"], p["token1"]), (p["token1"], p["token0"])):
            if a not in usd and usd.get(b) and st["L"] > 0:
                m0, m1 = meta[p["token0"]], meta[p["token1"]]
                p01 = st["sqrtP"] ** 2 * 10 ** m0["decimals"] / 10 ** m1["decimals"]
                usd[a] = usd[b] * (p01 if a == p["token0"] else 1 / p01)
    usd = {k: v for k, v in usd.items() if v}

    rows = [analyze_pool(p, states[p["pool"]], meta, usd, cfg, xcat) for p in pools]
    rows = [r for r in rows if (r["tvl_usd"] or 0) >= cfg["min_tvl_usd"]]
    for r in rows:
        r["dex_name"] = dexes[r["dex"]]["name"]
    by_pool = {p["pool"]: p for p in pools}

    # ---- 参考价与偏离
    stable_quoted = lambda r: by_pool[r["pool"]]["token0"] in cfg["stables"] or by_pool[r["pool"]]["token1"] in cfg["stables"]
    if eth:
        for r in rows:
            if r["base_addr"] == weth and stable_quoted(r):
                r.update(ref_price=eth["price"], ref_source="cex", price_dev_pct=(r["price"] / eth["price"] - 1) * 100)
    xrows = [r for r in rows if r["base_addr"] in xcat and stable_quoted(r)]
    ratios = wrapper_ratios([r["base_addr"] for r in xrows if xcat[r["base_addr"]]["kind"] == "wrapper"])
    stage("reference prices")
    refs = {}
    for r in xrows:
        info = xcat[r["base_addr"]]
        if info["symbol"] not in refs:   # 同一个 xStock 在多个 DEX 上只查一次
            refs[info["symbol"]] = xstock_reference(info["symbol"], info["native"], cfg["usdc"], cfg["rfq_size_usdc"])
            time.sleep(1.1)   # RFQ 限速 60 次/分钟
        ref = refs[info["symbol"]]
        ratio = ratios.get(r["base_addr"]) or 1.0
        r["xstock"] = {**ref, "wrapper_ratio": ratio}
        fresh = ref["source"] == "rfq" or (ref["kline_age_s"] is not None and ref["kline_age_s"] <= cfg["alerts"]["xstock_ref_max_age_s"])
        if ref["price"]:
            ref_wrapped = ref["price"] * ratio      # 换算成 1 份包装币的参考价
            r.update(ref_price=ref_wrapped, ref_source=ref["source"] + ("" if fresh else "(stale)"),
                     price_dev_pct=(r["price"] / ref_wrapped - 1) * 100 if fresh else None,
                     price_dev_pct_raw=(r["price"] / ref_wrapped - 1) * 100)

    # ---- 事件：交易量、Mint/Burn
    def usd_of(p, a0, a1, kind):
        u0, u1 = usd.get(p["token0"]), usd.get(p["token1"])
        m0, m1 = meta[p["token0"]], meta[p["token1"]]
        v0 = abs(a0) / 10 ** m0["decimals"] * u0 if u0 else None
        v1 = abs(a1) / 10 ** m1["decimals"] * u1 if u1 else None
        if kind == "swap":   # 取计价侧（quote）的金额作为名义成交额
            q_is1 = orient(p, meta, cfg, xcat)
            return (v1 if q_is1 else v0) or v0 or v1 or 0
        return (v0 or 0) + (v1 or 0)
    stage("events")
    sync_events(store, [by_pool[r["pool"]] for r in rows], head, cfg["event_backfill_days"], usd_of)
    for r in rows:
        for label, days in (("events_24h", 1), ("events_7d", 7)):
            ev = {}
            for kind, n, total, mx in store.event_stats(r["pool"], head - days * BLOCKS_PER_DAY):
                ev[f"{kind}_n"], ev[f"{kind}_usd"], ev[f"{kind}_max_usd"] = n, total, mx
            r[label] = ev
        if r["tvl_usd"]:
            r["turnover_24h_pct"] = r["events_24h"].get("swap_usd", 0) / r["tvl_usd"] * 100
            r["fee_apr_7d_pct"] = r["events_7d"].get("swap_usd", 0) / 7 * 365 * r["fee_pct"] / 100 / r["tvl_usd"] * 100

    # ---- LP 集中度
    labels = {k.lower(): v for k, v in cfg.get("lp_labels", {}).items()}
    lp_done = not args.skip_positions and stage("lp positions", optional=True)
    if lp_done:
        for d in dexes.values():
            drows = [r for r in rows if r["dex"] == d["id"]]
            if not drows:
                continue
            if d["type"] == "lb":
                for r in drows:
                    p = by_pool[r["pool"]]
                    created = store.get(f"lb_created:{p['pool']}") or lb.first_block(p["pool"], head)
                    store.put(f"lb_created:{p['pool']}", created)
                    v = r.get("_val")
                    hs = [{"owner": o, "label": labels.get(o), "positions": 1,
                           "usd": x / v["d"][0] * v["u"][0] + y / v["d"][1] * v["u"][1]}
                          for o, (x, y) in lb.holders(store, p, states[p["pool"]], head, created - 1).items()] if v else []
                    hs.sort(key=lambda h: -h["usd"])
                    for h in hs:
                        h["share_pct"] = h["usd"] / r["tvl_usd"] * 100 if r["tvl_usd"] else None
                    r["lp"] = lp_from_holders(hs)
            elif d.get("npm") and d["discovery"] == "events":   # NPM 全量扫描只适合持仓 NFT 不多的 DEX
                keys = {(by_pool[r["pool"]]["token0"], by_pool[r["pool"]]["token1"], by_pool[r["pool"]]["fee"]): r["pool"] for r in drows}
                sync_positions(store, d["npm"], keys)
                for r in drows:
                    p = by_pool[r["pool"]]
                    pos = store.positions(r["pool"])
                    r["lp"] = lp_concentration(pos, r["tick"], orient(p, meta, cfg, xcat))
                    if r.get("_val"):
                        r["lp"]["holders"] = lp_holders(pos, r["_val"], r["tvl_usd"], labels)

    # ---- SOW 指定池子：TVL 与 LP 构成
    designated = designated_summary(rows, cfg, labels, xcat)

    # ---- Fluxion RFQ 流动性与价格深度探测
    rfq_data = None
    rfq_cfg = cfg.get("rfq_depth", {})
    if rfq_cfg.get("enabled", True) and not getattr(args, "skip_rfq_depth", False) and stage("rfq depth", optional=True):
        rfq_symbols = cfg.get("designated", {}).get("pairs") or sorted(refs.keys())
        rfq_native_map = {}
        for a, info in xcat.items():
            if info["kind"] == "native" and info["symbol"] in rfq_symbols:
                rfq_native_map[info["symbol"]] = a

        depth_sizes = rfq_cfg.get("depth_sizes_usd", [1000, 10000, 50000, 100000])
        sell_sizes = rfq_cfg.get("sell_sizes_usd", [1000, 10000])
        delay = rfq_cfg.get("delay_s", 1.05)

        flux_xrows = {xcat[r["base_addr"]]["symbol"]: r for r in rows if r["dex"] == "fluxion" and r["base_addr"] in xcat and r["quote"] == "USDC"}

        rfq_items = []
        session_info = get_trading_session()
        sla_cfg = cfg.get("rfq_sla", {})
        sla_tiers = sla_cfg.get("tiers", {})
        sla_bms = sla_cfg.get("benchmarks", {}).get(session_info["session"], {})
        for sym in rfq_symbols:
            native = rfq_native_map.get(sym)
            if not native:
                continue
            base_ref = refs.get(sym)
            item = probe_rfq_depth(sym, native, cfg["usdc"], depth_sizes, sell_sizes, delay, base_ref=base_ref)

            tier = sla_tiers.get(sym, "Tier 2")
            item["tier"] = tier
            bm = sla_bms.get(tier, [20.0, 40.0])
            item["sla_bm_mid_bps"] = bm
            item["sla_bm_spread_bps"] = [round(b * 2, 1) for b in bm]

            if item["status"] != "active":
                if session_info["is_open"]:
                    item["sla_status"] = "missing_open"
                    item["sla_text_zh"] = "盘中缺报价"
                    item["sla_text_en"] = "Missing in Open"
                else:
                    item["sla_status"] = "unquoted_off_hours"
                    item["sla_text_zh"] = "盘前盘后无报价" if session_info["session"] == "pre_post" else "周末无报价"
                    item["sla_text_en"] = "Unquoted Off-Hours"
            else:
                actual_mid_bps = item["spread_bps"] / 2 if item.get("spread_bps") is not None else None
                item["actual_mid_bps"] = round(actual_mid_bps, 1) if actual_mid_bps is not None else None
                if actual_mid_bps is None:   # 只有买价、没有卖价，算不出点差
                    item["sla_status"] = "no_bid"
                    item["sla_text_zh"] = "缺卖价"
                    item["sla_text_en"] = "No Bid"
                elif actual_mid_bps <= bm[1]:
                    item["sla_status"] = "pass"
                    item["sla_text_zh"] = "达标"
                    item["sla_text_en"] = "Pass"
                else:
                    item["sla_status"] = "wide"
                    diff = actual_mid_bps - bm[1]
                    item["sla_text_zh"] = f"超标 (+{diff:.1f}bps)"
                    item["sla_text_en"] = f"Wide (+{diff:.1f}bps)"

            amm_row = flux_xrows.get(sym)
            if amm_row:
                ratio = ratios.get(amm_row["base_addr"]) or 1.0
                amm_native_px = (amm_row["price"] / ratio) if amm_row.get("price") and ratio else None
                mid = item.get("mid_price") or item.get("ask_1k")
                d2 = amm_row.get("depth", {}).get("2")
                d2_val = min(d2["up"], d2["down"]) if d2 else None
                item["amm"] = {
                    "pool": amm_row["pool"],
                    "pair": amm_row["pair"],
                    "fee_pct": amm_row["fee_pct"],
                    "tvl_usd": amm_row["tvl_usd"],
                    "depth_2pct_usd": d2_val,
                    "price": amm_row["price"],
                    "native_price": amm_native_px,
                    "wrapper_ratio": ratio,
                    "impact_10k_buy": amm_row["impact"].get("10000", {}).get("buy"),
                    "dev_pct": ((amm_native_px / mid - 1) * 100) if (amm_native_px and mid) else None
                }
            else:
                item["amm"] = None
            rfq_items.append(item)

        active_items = [it for it in rfq_items if it["status"] == "active"]
        avg_spread = (sum(it["spread_bps"] for it in active_items if it.get("spread_bps") is not None) / len(active_items)) if active_items else None
        rfq_data = {
            "ts": now,
            "session": session_info,
            "sizes_usd": depth_sizes,
            "sell_sizes_usd": sell_sizes,
            "summary": {
                "total": len(rfq_items),
                "active": len(active_items),
                "no_quote": len(rfq_items) - len(active_items),
                "avg_spread_bps": round(avg_spread, 1) if avg_spread else None,
                "avg_mid_bps": round(avg_spread / 2, 1) if avg_spread else None,
                "sla_pass_count": sum(1 for it in rfq_items if it.get("sla_status") == "pass"),
                "max_depth_usd": max([it.get("max_buy_usd", 0) for it in active_items] or [0])
            },
            "items": rfq_items
        }

        # ---- 跨链比价：同一 xStock 在 Solana（Jupiter）上的可成交报价
        if cfg.get("venue_compare", {}).get("enabled", True) and not getattr(args, "skip_venue", False) and stage("venue compare", optional=True):
            try:
                rfq_data["venue_summary"] = venue.run(store, rfq_items, cfg, session_info, now)
            except Exception as e:
                print("venue compare failed:", e)
        store.save_rfq_snapshot(now, {it["symbol"]: venue.history_row(it, session_info["session"]) for it in rfq_items})
        sla.record(store, rfq_items, session_info, now)

    # ---- xStocks 流通量与交易所充提
    flow_data = cp_data = None
    fcfg = cfg.get("flows", {})
    if fcfg.get("enabled", True) and stage("flows", optional=True):
        try:
            syms = cfg.get("designated", {}).get("pairs") or []
            natives = {info["symbol"]: a for a, info in xcat.items() if info["kind"] == "native" and info["symbol"] in syms}
            prices = {s: (refs.get(s) or {}).get("price") for s in natives}
            for it in (rfq_data or {}).get("items", []):
                prices[it["symbol"]] = prices.get(it["symbol"]) or it.get("mid_price")
            print("xfers synced:", flows.sync(store, natives, head))
            flow_data = flows.summarize(store, natives, fcfg.get("labels", {}), prices, head, now)
        except Exception as e:
            print("flows failed:", e)
        try:   # 充提对手方画像；公开产物不用私有的做市商名单，避免间接暴露承诺方
            known = () if public else [*cfg.get("designated", {}).get("committed_owners", []), *cfg.get("lp_labels", {})]
            cp_data = counterparty.classify(store, fcfg.get("labels", {}), xcat, prices, head, now, known,
                                            lp_npm=dexes.get("fluxion", {}).get("npm"))
        except Exception as e:
            print("counterparty failed:", repr(e))
            cp_data = {"ts": now, "error": "counterparty classification failed"}   # 页面显示暂不可用，而不是 404

    stage("alerts")
    # ---- 告警（去重：仅新触发 / 恢复时推送）
    a = cfg["alerts"]
    fired = {}
    for r in rows:
        prev = store.previous_snapshot(r["pool"], now, a["depth_drop_lookback_s"])
        for key, sev, code, params in evaluate(r, prev, cfg):
            fired[key] = (sev, message_zh(code, params), code, params)
    for key, sev, code, params in designated_alerts(designated, cfg):
        fired[key] = (sev, message_zh(code, params), code, params)
    active = store.active_alerts()
    if not lp_done:   # 本轮没算 LP 持仓：依赖它的告警保持上一轮状态，避免误报"已恢复"或承诺方资金为 0
        lp_dep = lambda k: k.startswith("designated:") or ":lp_conc" in k or ":range_edge:" in k
        fired = {k: v for k, v in fired.items() if not lp_dep(k)}
        fired.update({k: (row[1], row[2], None, None) for k, row in active.items() if lp_dep(k)})
    new_lines, resolved_lines = [], []   # [(key, 文案, 级别)]
    rank = {"info": 0, "warn": 1, "critical": 2}
    for key, (sev, msg, _, _) in fired.items():
        if key not in active or rank.get(sev, 0) > rank.get(active[key][1], 0):   # 新触发，或级别升级
            new_lines.append((key, f"[{sev.upper()}] {msg}", sev))
        store.set_alert(key, sev, msg, now)
    for key, row in active.items():
        if key not in fired:
            store.resolve_alert(key, now)
            resolved_lines.append((key, f"[已恢复] {row[2]}", row[1]))
    for r in rows:
        r.pop("_val", None)
    store.save_snapshot(now, head, rows)

    report = {"ts": now, "block": head, "eth_usd": eth, "pools": rows, "designated": designated, "rfq": rfq_data, "flows": flow_data, "lp_labels": labels,
              "dexes": [{"id": d["id"], "name": d["name"], "type": d["type"],
                         "pools_tracked": sum(1 for p in pools if p["dex"] == d["id"]),
                         "pools_shown": sum(1 for r in rows if r["dex"] == d["id"])} for d in dexes.values()],
              "alerts": [{"key": k, "severity": s, "message": m, "code": c, "params": p} for k, (s, m, c, p) in fired.items()]}
    pub = public_view(report, cfg, xcat) if public else report
    out_dir = ROOT / "data" / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "latest.json").write_text(json.dumps(clean(pub), indent=1, ensure_ascii=False))
    (out_dir / "latest.md").write_text(render_md(pub))
    (out_dir / "counterparties.json").write_text(json.dumps(clean(cp_data or {"ts": now, "error": "flows disabled"}), indent=1, ensure_ascii=False))
    try:   # SLA 记录：追加到长期 CSV（CI 中为 sla-data 分支）并汇总
        sla_dir = Path(os.environ.get("FLUXMON_SLA_DIR") or ROOT / "data" / "sla")
        print("sla rows appended:", sla.export(store, sla_dir))
        (out_dir / "sla.json").write_text(json.dumps(clean(sla.summarize(sla_dir, now)), separators=(",", ":")))
        print("rfq series:", sla.export_series(sla_dir, out_dir / "rfq", now))
    except Exception as e:
        print("sla export failed:", repr(e))
        (out_dir / "sla.json").write_text(json.dumps({"ts": now, "error": "sla summary failed"}))
    (out_dir / "history.json").write_text(json.dumps(clean(store.export_history()), separators=(",", ":")))
    print(render_console(pub))
    changes = new_lines + resolved_lines
    if changes:
        print("\n".join(["", "== 告警变化 =="] + [l for k, l, _ in changes if not (public and k.startswith("designated:"))]))
        if not args.no_notify:   # 推送只发 critical 的触发 / 升级 / 恢复，其余看看板
            notify(cfg, [l for _, l, sev in changes if sev == "critical"])
    if not args.no_notify and mailer.configured():
        ecfg = cfg.get("email", {})
        mailer.critical_alerts(store, fired, now, ecfg)
        if args.daily_now or mailer.daily_due(store, now, ecfg):
            mailer.daily_report(store, report, now, ecfg)


def _f(v, fmt="{:,.0f}", none="-"):
    return none if v is None else fmt.format(v)


def render_console(rep):
    lines = [f"Mantle 流动性快照  block {rep['block']}  ETH(CEX) ${_f((rep['eth_usd'] or {}).get('price'), '{:,.2f}')}",
             f"{'DEX':<14}{'池子':<22}{'费率':>6}{'TVL':>13}{'±2%上':>10}{'±2%下':>10}{'±10%内占比':>11}{'$10k买冲击':>11}{'24h量':>11}{'偏离':>8}{'Top1 LP':>9}"]
    for r in sorted(rep["pools"], key=lambda r: -(r["tvl_usd"] or 0)):
        d2 = r["depth"].get("2", {})
        imp = r["impact"].get("10000", {}).get("buy")
        lines.append(f"{r['dex_name']:<14}{r['pair']:<22}{r['fee_pct']:>5}%{_f(r['tvl_usd'], '${:,.0f}'):>13}{_f(d2.get('up'), '${:,.0f}'):>10}"
                     f"{_f(d2.get('down'), '${:,.0f}'):>10}{_f(r.get('in_band_10_pct'), '{:.1f}%'):>11}{_f(imp, '{:.2f}%', '不足'):>11}"
                     f"{_f(r['events_24h'].get('swap_usd', 0), '${:,.0f}'):>11}{_f(r.get('price_dev_pct'), '{:+.2f}%'):>8}"
                     f"{_f((r.get('lp') or {}).get('top1_pct'), '{:.0f}%'):>9}")
    if rep.get("rfq"):
        rfq = rep["rfq"]
        sm = rfq["summary"]
        sess = rfq.get("session") or {}
        sess_title = f"{sess.get('name_zh', '未知时段')} · {sess.get('et_time', '')}"
        lines.append("")
        lines.append(f"Fluxion RFQ 流动性与价格深度 [{sess_title}]")
        lines.append(f"  在线 {sm['active']}/{sm['total']} · SLA达标 {sm.get('sla_pass_count', 0)}/{sm['total']} · 均价差 {_f(sm.get('avg_spread_bps'), '{:.1f} bps')} (mid {_f(sm.get('avg_mid_bps'), '{:.1f} bps')}) · 最大承接 ${_f(sm.get('max_depth_usd'), '{:,.0f}')}")
        lines.append(f"{'标的':<8}{'层级':<8}{'状态':<10}{'SLA承诺(mid)':>14}{'实际(mid)':>11}{'买卖价差':>10}{'SLA判定':>12}{'RFQ $10k':>10}{'RFQ $100k':>11}{'AMM $10k':>10}{'AMM偏离':>9}")
        for it in rfq["items"]:
            stat = "正常" if it["status"] == "active" else f"无报价({it.get('http_status')})"
            tier = it.get("tier", "-")
            bm = f"{it['sla_bm_mid_bps'][0]:.0f}-{it['sla_bm_mid_bps'][1]:.0f} bps" if it.get("sla_bm_mid_bps") else "-"
            mid_bps = f"{it['actual_mid_bps']:.1f} bps" if it.get("actual_mid_bps") is not None else "-"
            sp = f"{it['spread_bps']:.1f} bps" if it.get("spread_bps") is not None else "-"
            sla_text = it.get("sla_text_zh", "-")
            b10 = _f(it.get("buy_depth", {}).get("10000", {}).get("impact_pct"), "{:+.2f}%")
            b100 = _f(it.get("buy_depth", {}).get("100000", {}).get("impact_pct"), "{:+.2f}%")
            amm = it.get("amm") or {}
            amm_imp = _f(amm.get("impact_10k_buy"), "{:.2f}%", "不足") if amm else "-"
            amm_dev = _f(amm.get("dev_pct"), "{:+.2f}%") if amm else "-"
            lines.append(f"{it['symbol']:<8}{tier:<8}{stat:<10}{bm:>14}{mid_bps:>11}{sp:>10}{sla_text:>12}{b10:>10}{b100:>11}{amm_imp:>10}{amm_dev:>9}")
    return "\n".join(lines)


def render_md(rep):
    ts = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(rep["ts"]))
    md = [f"# Mantle 流动性快照（{ts}）", "",
          f"区块 {rep['block']}，ETH 参考价（CEX）${_f((rep['eth_usd'] or {}).get('price'), '{:,.2f}')}", "",
          "| DEX | 池子 | 费率 | TVL | ±2% 上 | ±2% 下 | ±10% 内占 TVL | $10k 买入冲击 | 24h 成交 | 7d 费率 APR | 偏离参考价 | Top1 LP |",
          "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for r in sorted(rep["pools"], key=lambda r: -(r["tvl_usd"] or 0)):
        d2 = r["depth"].get("2", {})
        md.append("| " + " | ".join([
            r["dex_name"], r["pair"], f"{r['fee_pct']}%", _f(r["tvl_usd"], "${:,.0f}"), _f(d2.get("up"), "${:,.0f}"), _f(d2.get("down"), "${:,.0f}"),
            _f(r.get("in_band_10_pct"), "{:.1f}%"), _f(r["impact"].get("10000", {}).get("buy"), "{:.2f}%", "流动性不足"),
            _f(r["events_24h"].get("swap_usd", 0), "${:,.0f}"), _f(r.get("fee_apr_7d_pct"), "{:.2f}%"),
            _f(r.get("price_dev_pct", r.get("price_dev_pct_raw")), "{:+.2f}%") + ("" if r.get("price_dev_pct") is not None or r.get("price_dev_pct_raw") is None else "（参考价过期）"),
            _f((r.get("lp") or {}).get("top1_pct"), "{:.1f}%")]) + " |")
    if rep.get("rfq"):
        rfq = rep["rfq"]
        sm = rfq["summary"]
        sess = rfq.get("session") or {}
        sess_name = sess.get("name_zh", "")
        et_time = sess.get("et_time", "")
        md += [
            "",
            "## Fluxion RFQ 流动性与价格深度",
            "",
            f"- **当前美股时段**：**{sess_name}**（美东时间 `{et_time}`）",
            f"- **做市概况**：在线 **{sm['active']}/{sm['total']}**，SLA 达标 **{sm.get('sla_pass_count', 0)}/{sm['total']}**，平均买卖价差 **{_f(sm.get('avg_spread_bps'), '{:.1f} bps')}**（折合中价点差 {_f(sm.get('avg_mid_bps'), '{:.1f} bps')}），单笔最高承接 **${_f(sm.get('max_depth_usd'), '{:,.0f}')}**（0 滑点）。",
            "",
            "| 标的 | Tier | RFQ 状态 | SLA 承诺 (from mid) | 实际点差 (mid) | 买卖价差 (Spread) | SLA 判定 | 买价 Ask ($1k) | 卖价 Bid ($1k) | RFQ $10k 冲击 | RFQ $50k 冲击 | RFQ $100k 冲击 | AMM $10k 买入冲击 | AMM 偏离 RFQ |",
            "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
        ]
        for it in rfq["items"]:
            stat = "正常" if it["status"] == "active" else f"无报价 ({it.get('http_status')})"
            tier = it.get("tier", "-")
            bm = f"{it['sla_bm_mid_bps'][0]:.0f}–{it['sla_bm_mid_bps'][1]:.0f} bps" if it.get("sla_bm_mid_bps") else "-"
            mid_bps = f"{it['actual_mid_bps']:.1f} bps" if it.get("actual_mid_bps") is not None else "-"
            sp = f"{it['spread_bps']:.1f} bps" if it.get("spread_bps") is not None else "-"
            sla_text = it.get("sla_text_zh", "-")
            ask = _f(it.get("ask_1k"), "${:,.2f}")
            bid = _f(it.get("bid_1k"), "${:,.2f}")
            b10 = _f(it.get("buy_depth", {}).get("10000", {}).get("impact_pct"), "{:+.2f}%")
            b50 = _f(it.get("buy_depth", {}).get("50000", {}).get("impact_pct"), "{:+.2f}%")
            b100 = _f(it.get("buy_depth", {}).get("100000", {}).get("impact_pct"), "{:+.2f}%")
            amm = it.get("amm") or {}
            amm_imp = _f(amm.get("impact_10k_buy"), "{:.2f}%", "流动性不足") if amm else "-"
            amm_dev = _f(amm.get("dev_pct"), "{:+.2f}%") if amm else "-"
            md.append(f"| {it['symbol']} | {tier} | {stat} | {bm} | {mid_bps} | {sp} | {sla_text} | {ask} | {bid} | {b10} | {b50} | {b100} | {amm_imp} | {amm_dev} |")
    vs = (rep.get("rfq") or {}).get("venue_summary")
    if vs:
        w = lambda k: f"Fluxion {vs[k]['flux']} / Jupiter {vs[k]['jup']} / 持平 {vs[k]['tie']}"
        md += [
            "",
            "## 跨链比价：Fluxion RFQ vs Solana（Jupiter）",
            "",
            "成本单位 bps，相对美股最新成交价（越低越好）；优势 = Fluxion 相对 Jupiter 省多少（正 = Fluxion 更优）。Solana 价格已按 Scaled UI multiplier 换算成每股。",
            "",
            f"- **$10k 买入更优**：{w('buy10')}；**$100k 买入更优**：{w('buy100')}；**$10k 卖出更优**：{w('sell10')}",
            "",
            "| 标的 | 价差 F / J | 买入 $10k F / J | 买入 $100k F / J | 卖出 $10k F / J | 优势 买$10k | 优势 买$100k | 优势 卖$10k |",
            "|---|---|---|---|---|---|---|---|",
        ]
        pair = lambda v, a, b: f"{_f(v.get(a), '{:.1f}')} / {_f(v.get(b), '{:.1f}')}"
        for it in rep["rfq"]["items"]:
            v = it.get("venue")
            if not v:
                continue
            md.append(f"| {it['symbol']} | {pair(v, 'f_spread', 'j_spread')} | {pair(v, 'f_buy10', 'j_buy10')} | {pair(v, 'f_buy100', 'j_buy100')} | "
                      f"{pair(v, 'f_sell10', 'j_sell10')} | {_f(v.get('buy_adv10'), '{:+.1f}')} | {_f(v.get('buy_adv100'), '{:+.1f}')} | {_f(v.get('sell_adv10'), '{:+.1f}')} |")
    fl = rep.get("flows")
    if fl:
        t = fl["totals"]
        md += ["", "## xStocks 流通量与 Bybit 充提", "",
               f"- **总供应** ${t['supply_usd']:,.0f}，其中发行方库存 ${t['issuer_usd']:,.0f}，**流通** ${t['circulating_usd']:,.0f}"
               f"（交易所钱包 ${t['exchange_usd']:,.0f}，链上用户与合约 ${t['onchain_usd']:,.0f}）", "",
               "| 窗口 | 提现（交易所→链上） | 充值（链上→交易所） | 净流入链上 | 链上转账总额 |", "|---|---|---|---|---|"]
        for k, w in fl["windows"].items():
            md.append(f"| {k} | ${w['wd_usd']:,.0f}（{w['wd_n']} 笔 / {w['wd_addrs']} 地址） | ${w['dp_usd']:,.0f}（{w['dp_n']} 笔 / {w['dp_addrs']} 地址） | "
                      f"${w['net_usd']:+,.0f} | ${w['xfer_usd']:,.0f} |")
    md += ["", "## 当前告警", ""] + [f"- **{a['severity']}** {a['message']}" for a in rep["alerts"]]
    return "\n".join(md) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Mantle 流动性监控（DEX 池子 / RFQ / 跨链比价 / xStocks 充提）")
    ap.add_argument("--no-notify", action="store_true", help="不推送飞书 webhook / 邮件")
    ap.add_argument("--skip-positions", action="store_true", help="跳过 LP 持仓同步（首次全量较慢）")
    ap.add_argument("--skip-rfq-depth", action="store_true", help="跳过 RFQ 多档深度探测")
    ap.add_argument("--skip-venue", action="store_true", help="跳过 Solana（Jupiter）跨链比价")
    ap.add_argument("--daily-now", action="store_true", help="立即发送邮件日报（忽略发送时间）")
    run(ap.parse_args())


if __name__ == "__main__":
    main()
