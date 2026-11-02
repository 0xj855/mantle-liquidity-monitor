"""最小化 JSON-RPC / Multicall3 / getLogs 工具，零第三方依赖。"""
import json
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

RPC_URL = "https://rpc.mantle.xyz"
MULTICALL3 = "0xcA11bde05977b3631167028862bE2a173976CA11"
LOG_RANGE = 10_000          # rpc.mantle.xyz 单次 getLogs 的区块上限
BLOCKS_PER_DAY = 43_200     # Mantle 出块 2s


def rpc(method, params, url=None, timeout=20):
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    req = urllib.request.Request(url or RPC_URL, body, {"content-type": "application/json"})
    data = json.load(urllib.request.urlopen(req, timeout=timeout))
    if "error" in data:
        raise RuntimeError(data["error"])
    return data["result"]


def retry(fn, tries=4):
    """最坏约 tries × 20s 超时 + 7s 退避；RPC 长时间不可用时尽快失败，而不是把整轮拖到超时。"""
    for i in range(tries):
        try:
            return fn()
        except Exception:
            if i == tries - 1:
                raise
            time.sleep(min(2 ** i, 8))


def block_number():
    return int(retry(lambda: rpc("eth_blockNumber", [])), 16)


def block_timestamp(n):
    return int(retry(lambda: rpc("eth_getBlockByNumber", [hex(n), False]))["timestamp"], 16)


# ---------- ABI helpers ----------
def word(v):
    return format(v & (2 ** 256 - 1), "064x")


def addr_word(a):
    return a[2:].lower().rjust(64, "0")


def words(h):
    h = h[2:]
    return [h[i:i + 64] for i in range(0, len(h), 64)]


def sint(h, bits=256):
    v = int(h, 16)
    return v - 2 ** bits if v >= 2 ** (bits - 1) else v


def decode_string(h):
    b = bytes.fromhex(h[2:])
    try:
        n = int.from_bytes(b[32:64], "big")
        return b[64:64 + n].decode()
    except Exception:
        return b[:32].rstrip(b"\0").decode(errors="ignore")


# ---------- Multicall3.aggregate3 ----------
def _encode_aggregate3(calls):
    n = len(calls)
    heads, tails, off = [], [], 32 * n
    for to, data in calls:
        b = bytes.fromhex(data[2:])
        padded = b + b"\0" * ((-len(b)) % 32)
        t = addr_word(to) + word(1) + word(96) + word(len(b)) + padded.hex()
        heads.append(word(off))
        tails.append(t)
        off += len(t) // 2
    return "0x82ad56cb" + word(32) + word(n) + "".join(heads) + "".join(tails)


def _decode_aggregate3(h):
    b = bytes.fromhex(h[2:])
    g = lambda o: int.from_bytes(b[o:o + 32], "big")
    base = g(0)
    out = []
    for i in range(g(base)):
        eo = base + 32 + g(base + 32 + 32 * i)
        ok = g(eo)
        do = eo + g(eo + 32)
        n = g(do)
        out.append("0x" + b[do + 32:do + 32 + n].hex() if ok else None)
    return out


def multicall(calls, block="latest", chunk=150):
    """calls: [(to, calldata)] -> [returndata | None(调用失败)]"""
    res = []
    for i in range(0, len(calls), chunk):
        data = _encode_aggregate3(calls[i:i + chunk])
        res += _decode_aggregate3(retry(lambda: rpc("eth_call", [{"to": MULTICALL3, "data": data}, block])))
    return res


def get_logs(address, topics, start, end, threads=3):
    def one(b):
        flt = {"address": address, "fromBlock": hex(b), "toBlock": hex(min(b + LOG_RANGE - 1, end)), "topics": topics}
        return retry(lambda: rpc("eth_getLogs", [flt]))
    if end < start:
        return []
    with ThreadPoolExecutor(threads) as ex:
        return sum(ex.map(one, range(start, end + 1, LOG_RANGE)), [])
