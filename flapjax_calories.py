#!/usr/bin/env python3
"""flapjax-calories - what the FlapJax treasury actually fed one wallet, and
whether that wallet can still be fed at 1.2x today.

Read-only. No keys, no signatures, no transactions. Standard library only.

  python3 flapjax_calories.py ledger 0xWALLET [--days 7] [--json]
  python3 flapjax_calories.py hold   0xWALLET [--days 30] [--json]

Why the tool exists: the FlapJax board pays from one treasury address, caps an
agent at 3 paid claims per COT day, and pays 1.2x to wallets that still hold at
least 80% of what the treasury sent them. All three facts are on-chain, but a
claimant normally reads them by hand, in UTC, one BscScan tab at a time. This
tool answers the three questions in one command and, where the chain cannot
answer, says so instead of guessing.

Exit codes: 0 ok, 2 bad input, 3 no provider answered, 4 log and receipt read
disagree.
"""

import json
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone

TOKEN = "0x90c8889f428F9Ebb77BB8f15CAD3a50a9aC680df"
TREASURY = "0x7C34E9e21eE28A49Ff0b84B61774119E6633359f"
TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"

# COT is fixed UTC-5 (no DST). Verified against the board's own pair: treasury
# 824,532,279 FLAPJAX "at block 130,953,649 (2026-10-31 19:33 COT)" and that
# block's timestamp is 2026-11-01T00:33:02Z. If that pair ever stops agreeing,
# this constant is the first thing to re-check.
COT = timezone(timedelta(hours=-5))

# Board rails, in FJ, as published on flapjax.surge.sh (Bounty Board).
RAILS = [
    ("sip", 300_000),
    ("post", 10_000),
    ("reach", 250_000),
    ("meme", 2_000_000),
    ("dogma", 3_000_000),
    ("tool", 15_000_000),
    ("ref", 100_000),
]
HOLDER_MULTIPLIER = 1.2          # 80%-hold bonus, checked by balanceOf
HOLD_THRESHOLD = 0.8
DUST = 10 ** 15                  # payouts carry sub-0.001 FJ rounding, e.g.
                                 # 300000.000000000008 FJ; exact equality lies

# Weekly slot caps as published on the board (None = no weekly cap printed).
SLOTS = {"sip": 700, "post": 700, "reach": 10, "meme": 5, "dogma": 4,
         "tool": 1, "ref": 350}
PAID_CLAIMS_PER_COT_DAY = 3      # "3 paid claims per day" (board rules)

RPCS = [
    "https://rpc.48.club",
    "https://bsc-dataseed.binance.org",
    "https://bsc-dataseed1.defibit.io",
    "https://bsc-dataseed1.ninicoin.io",
]
WINDOW = 5_000                   # public BSC nodes cap eth_getLogs at 5k blocks


class ChainError(Exception):
    """No provider answered, or all of them answered with an error."""


# ---------------------------------------------------------------- address ----

def parse_address(raw):
    """Return a lower-case 40-hex address, or raise ValueError with the reason.

    A 39- or 41-character address is the failure that gets a claim refused, so
    the length is checked before the hex and named in the error.
    """
    if not isinstance(raw, str):
        raise ValueError("address must be a string")
    s = raw.strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    if len(s) != 40:
        raise ValueError(
            "address has %d hex characters after 0x, need exactly 40 "
            "(39-char addresses are the classic copy/paste claim refusal)" % len(s))
    try:
        int(s, 16)
    except ValueError:
        raise ValueError("address contains non-hex characters")
    return "0x" + s.lower()


def topic_address(addr):
    return "0x" + addr.lower().replace("0x", "").rjust(64, "0")


# ------------------------------------------------------------------ units ----

def wei_to_fj(wei):
    """Exact FJ string from wei: FLAPJAX has 18 decimals. Never floats."""
    wei = int(wei)
    whole, frac = divmod(wei, 10 ** 18)
    frac_s = ("%018d" % frac).rstrip("0")
    return "%d.%s" % (whole, frac_s) if frac_s else "%d.0" % whole


def fj_to_float(text):
    return float(text)


def pretty_fj(wei, places=2):
    whole, frac = divmod(int(wei), 10 ** 18)
    frac_s = ("%018d" % frac)[:places]
    return "{:,}.{}".format(whole, frac_s)


# -------------------------------------------------------------- COT dates ----

def cot_datetime(utc_ts):
    return datetime.fromtimestamp(int(utc_ts), COT)


def cot_date(utc_ts):
    """COT calendar date for a UTC timestamp (COT = UTC-5, no DST)."""
    return cot_datetime(utc_ts).date()


def cot_day_start_ts(day):
    """UTC timestamp of 00:00 COT on `day`."""
    return int(datetime(day.year, day.month, day.day, tzinfo=COT).timestamp())


def window_bounds(now_ts, days):
    """(start_ts, end_ts, start_date, end_date) for the last `days` COT days,
    ending at the end of today's COT day."""
    end_day = cot_date(now_ts)
    start_day = end_day - timedelta(days=max(0, int(days)) - 1) if days >= 1 else end_day
    return (cot_day_start_ts(start_day), int(now_ts), start_day, end_day)


def cot_week_start(day):
    """Monday 00:00 COT of the week containing `day` (board: weeks run Mon 00:00 COT)."""
    return day - timedelta(days=day.weekday())


# ---------------------------------------------------------------- amounts ----

def classify_amount(wei):
    """Board rails an amount *could* be. Amount alone is not a rail: the 1.2x
    holder bonus makes 250k look like 300k and 15M look like 18M."""
    fj = int(wei)
    out = []
    for name, base in RAILS:
        base_wei = base * 10 ** 18
        if abs(fj - base_wei) <= DUST:
            out.append(name)
        elif abs(fj - base_wei * 12 // 10) <= DUST:  # integer math: 1.2 drifts wei
            out.append("%s@1.2x" % name)
    return out


def hold_ratio(balance_wei, received_wei):
    """Fraction of the received amount still held, or None when nothing was
    received in the window (a ratio of nothing is not 0%)."""
    if int(received_wei) <= 0:
        return None
    return int(balance_wei) / int(received_wei)


# ------------------------------------------------------------------- rpc ----

class Chain(object):
    """Thin JSON-RPC client over public read-only BSC endpoints."""

    def __init__(self, urls=None, timeout=25, jobs=3):
        self.urls = list(urls or RPCS)
        self.timeout = timeout
        self.jobs = max(1, int(jobs))
        self.used = {}
        self._lock = threading.Lock()
        self._good = {}

    def call(self, method, params, role="default", rounds=3):
        """Try every provider, then every provider again: public BSC endpoints
        drop TLS mid-request often enough that a single pass cries wolf."""
        last = None
        order = self.urls
        with self._lock:
            if role in self._good:                     # pin what worked: one
                good = self._good[role]                # provider answers getLogs
                order = [good] + [u for u in self.urls if u != good]
        for attempt in range(rounds):
            if attempt:
                time.sleep(1.0 + attempt)   # a node that just said "limit
                                            # exceeded" needs a breath, not a
                                            # different node
            for url in order:
                try:
                    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                                       "params": params}).encode()
                    req = urllib.request.Request(
                        url, data=body, headers={"Content-Type": "application/json"})
                    with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                        payload = json.loads(resp.read().decode("utf-8", "replace"))
                except Exception as exc:        # network, TLS, 403, timeout
                    last = exc
                    continue
                if payload.get("result") is not None:
                    with self._lock:
                        self.used.setdefault(role, url)
                        self._good.setdefault(role, url)
                    return payload["result"]
                last = payload.get("error")
        raise ChainError("%s failed on every provider: %s" % (method, last))

    def head(self):
        return int(self.call("eth_blockNumber", []), 16)

    def block_ts(self, number):
        blk = self.call("eth_getBlockByNumber", [hex(int(number)), False])
        return int(blk["timestamp"], 16)

    def block_at(self, utc_ts):
        """First block whose timestamp is >= utc_ts, by binary search.

        Assumes no block time: BSC moved from 3s to well under 1s per block, so
        any hardcoded blocks-per-day constant silently drops payouts.
        """
        lo, hi = 1, self.head()
        while lo < hi:
            mid = (lo + hi) // 2
            if self.block_ts(mid) < utc_ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def transfer_logs(self, start_block, end_block, frm=TREASURY, to=None,
                      token=TOKEN):
        """FLAPJAX Transfer logs with `frm` as sender.

        Windows are fetched concurrently (BSC now produces blocks in well under
        a second, so a COT week is ~270 windows of the 5,000-block cap) and the
        merged list is sorted by block so the result does not depend on timing.
        A window that never answers raises: a partial scan would print a total
        that is quietly wrong, which is exactly what this tool exists to stop.
        """
        wins, cur = [], int(end_block)
        while cur >= int(start_block):
            lo = max(int(start_block), cur - WINDOW + 1)
            wins.append((lo, cur))
            cur = lo - 1

        topics = [TRANSFER_TOPIC, topic_address(frm)]
        if to:
            topics.append(topic_address(to))

        def ask(lo, hi):
            return self.call("eth_getLogs", [{
                "fromBlock": hex(lo), "toBlock": hex(hi),
                "address": token, "topics": topics}])

        def fetch(win, depth=0):
            """One window, split on failure: a node that answers 'limit exceeded'
            for 5,000 blocks often answers 2,500, and splitting beats dropping the
            window and printing a total that is quietly short."""
            lo, hi = win
            try:
                return ask(lo, hi) or []
            except ChainError:
                if depth >= 3 or hi - lo < 1000:
                    raise ChainError("window %d-%d lost after %d splits" % (lo, hi, depth))
                mid = (lo + hi) // 2
                return fetch((lo, mid), depth + 1) + fetch((mid + 1, hi), depth + 1)

        logs = []
        if self.jobs == 1:
            for win in wins:
                logs.extend(fetch(win))
        else:
            with ThreadPoolExecutor(max_workers=self.jobs) as pool:
                for part in pool.map(fetch, wins):
                    logs.extend(part)
        logs.sort(key=lambda l: (int(l.get("blockNumber", "0x0"), 16),
                                 l.get("logIndex", "0x0")))
        return logs

    def receipt(self, tx_hash):
        return self.call("eth_getTransactionReceipt", [tx_hash], role="receipts")

    def balance_of(self, wallet, token=TOKEN):
        data = "0x70a08231" + topic_address(wallet)[2:]
        raw = self.call("eth_call", [{"to": token, "data": data}, "latest"])
        return int(raw, 16) if raw and raw != "0x" else 0


# ------------------------------------------------------------- decoding ----

def decode_transfer(log):
    """Raw log -> dict, or None if it is not a well-formed FLAPJAX Transfer."""
    try:
        topics = log["topics"]
        if len(topics) < 3:
            return None
        return {
            "block": int(log["blockNumber"], 16),
            "tx": log["transactionHash"],
            "wei": int(log["data"], 16),
            "from": "0x" + topics[1][-40:].lower(),
            "to": "0x" + topics[2][-40:].lower(),
        }
    except (KeyError, TypeError, ValueError):
        return None


def verify_with_receipt(row, receipt):
    """Cross-read one payout against its own receipt.

    Returns (ok, reason). ok only when the receipt exists, succeeded, and
    carries a FLAPJAX Transfer of exactly that amount from the treasury to the
    recipient in that block.
    """
    if not receipt:
        return False, "receipt missing"
    if int(receipt.get("status", "0x0"), 16) != 1:
        return False, "receipt status failed"
    if int(receipt.get("blockNumber", "0x0"), 16) != row["block"]:
        return False, "receipt block differs"
    for log in receipt.get("logs") or []:
        d = decode_transfer(log)
        if (d and d["from"] == row["from"] and d["to"] == row["to"]
                and d["wei"] == row["wei"] and d["tx"].lower() == row["tx"].lower()):
            return True, "transfer matches"
    return False, "no matching Transfer in receipt"


def build_rows(raw_logs, wallet):
    """Treasury -> wallet rows in block order, with COT attribution."""
    chain = None
    rows = []
    want = wallet.lower() if wallet else None
    for log in raw_logs:
        d = decode_transfer(log)
        if not d or d["from"] != TREASURY.lower():
            continue
        if want and d["to"] != want:
            continue
        rows.append(d)
    rows.sort(key=lambda r: (r["block"], r["tx"]))
    return rows


def annotate_times(chain, rows):
    """Attach the COT timestamp and COT date to each row."""
    cache = {}
    for r in rows:
        if r["block"] not in cache:
            cache[r["block"]] = chain.block_ts(r["block"])
        r["ts"] = cache[r["block"]]
        r["cot"] = cot_datetime(r["ts"])
        r["cot_day"] = r["cot"].date()
    return rows


# ----------------------------------------------------------- token index ----
# A public token index answers the same question as a few hundred
# eth_getLogs windows: which FLAPJAX transfers left the treasury and when. It
# costs one request and it does not throttle topic filters, which is what the
# free BSC nodes do after a sustained scan. The trade: the index returns no
# block number, so rows carry the index's own timestamp, and its coverage is
# only as deep as the pages you asked for. Both limits are printed, never
# papered over.

INDEX_HOST = "https://api.binplorer.com"
INDEX_KEY = "freekey"
INDEX_PAGE = 100


def index_url(address, limit=INDEX_PAGE, skip=0, key=INDEX_KEY):
    return "%s/getAddressHistory/%s?apiKey=%s&limit=%d&skip=%d" % (
        INDEX_HOST, address, key, limit, skip)


def fetch_index(address, limit=INDEX_PAGE, skip=0, timeout=25, rounds=3,
                opener=None):
    """One page of raw index JSON. Retried; ValueError if it never parses."""
    url = index_url(address, limit, skip)
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    last = None
    for _ in range(max(1, rounds)):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "flapjax-calories"})
            resp = opener.open(req, timeout=timeout)
            body = resp.read().decode("utf-8")
            if hasattr(resp, "close"):
                resp.close()
            return json.loads(body)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
            time.sleep(1.0)
    raise ValueError("token index did not answer: %s" % last)


def index_rows(payload, wallet=None, token=TOKEN, treasury=TREASURY):
    """Index operations -> treasury payouts, oldest first, COT attributed.

    Only FLAPJAX transfers actually sent by the treasury survive; other tokens
    on the same address, incoming transfers, and duplicate tx rows do not.
    """
    ops = payload.get("operations") if isinstance(payload, dict) else None
    want = wallet.lower() if wallet else None
    seen = set()
    rows = []
    for o in ops or []:
        if not isinstance(o, dict):
            continue
        info = o.get("tokenInfo") or {}
        if (info.get("address") or "").lower() != token.lower():
            continue
        frm = (o.get("from") or "").lower()
        to = (o.get("to") or "").lower()
        tx = (o.get("transactionHash") or "").lower()
        if frm != treasury.lower() or not tx or tx in seen:
            continue
        if want and to != want:
            continue
        try:
            ts = int(o.get("timestamp"))
            wei = int(o.get("value"))
        except (TypeError, ValueError):
            continue
        seen.add(tx)
        cot = cot_datetime(ts)
        rows.append({"block": None, "tx": tx, "wei": wei, "from": frm, "to": to,
                     "ts": ts, "cot": cot, "cot_day": cot.date()})
    rows.sort(key=lambda r: (r["ts"], r["tx"]))
    return rows


def index_depth(args):
    """Fetch the index and find out how deep it actually goes.

    The host answers one page of the newest 100 operations and IGNORES skip:
    skip=0/100/200 return the identical set. Asking for more pages therefore
    goes nowhere, so paging is probed once and reported instead of being
    assumed. Returns (ops, pages, paging_works, floor_ts).
    """
    first = fetch_index(TREASURY, limit=INDEX_PAGE, skip=0)
    ops = list((first.get("operations") or []) if isinstance(first, dict) else [])
    pages = 1
    paging_works = None
    if args["pages"] > 1 and len(ops) >= INDEX_PAGE:
        second = fetch_index(TREASURY, limit=INDEX_PAGE, skip=INDEX_PAGE)
        got = list((second.get("operations") or []) if isinstance(second, dict) else [])
        tx1 = {(o.get("transactionHash") or "").lower() for o in ops}
        tx2 = {(o.get("transactionHash") or "").lower() for o in got}
        paging_works = bool(got) and tx1 != tx2
        if paging_works:
            ops.extend(got)
            pages = 2
            while pages < args["pages"]:
                payload = fetch_index(TREASURY, limit=INDEX_PAGE, skip=pages * INDEX_PAGE)
                got = list((payload.get("operations") or []) if isinstance(payload, dict) else [])
                if not got:
                    break
                ops.extend(got)
                pages += 1
                if len(got) < INDEX_PAGE:
                    break
    return ops, pages, paging_works


def index_cmd(args):
    wallet = parse_address(args["wallet"])
    ops, pages, paging_works = index_depth(args)

    now_ts = int(time.time())
    start_ts, _end_ts, d0, d1 = window_bounds(now_ts, args["days"])
    all_rows = index_rows({"operations": ops})
    rows = [r for r in all_rows if r["ts"] >= start_ts]
    today = cot_date(now_ts)
    summary = summarize(rows, today, cot_week_start(today))
    tool_rows = [r for r in rows if "tool" in classify_amount(r["wei"])]
    reached_back = cot_datetime(all_rows[0]["ts"]).isoformat() if all_rows else None
    covered = bool(all_rows) and all_rows[0]["ts"] <= start_ts

    if args["json"]:
        print(json.dumps({
            "wallet": wallet,
            "source": {"host": INDEX_HOST, "pages": pages,
                       "paging_supported": paging_works,
                       "rows_from_treasury": len(all_rows),
                       "oldest_row_cot": reached_back,
                       "window_covered": covered},
            "window": {"from": d0.isoformat(), "to": d1.isoformat(),
                       "days": args["days"]},
            "payouts": [{"tx": r["tx"], "wei": str(r["wei"]), "fj": wei_to_fj(r["wei"]),
                         "to": r["to"], "cot_time": r["cot"].isoformat(),
                         "cot_day": r["cot_day"].isoformat(),
                         "rails_amount_only": classify_amount(r["wei"])}
                        for r in rows],
            "summary": summary,
            "tool_rail": {"payouts_of_tool_size": len(tool_rows),
                          "tx": [r["tx"] for r in tool_rows]},
        }, indent=2))
    else:
        print("wallet     %s" % wallet)
        print("window     %s .. %s COT (%.2f days)" % (d0.isoformat(), d1.isoformat(),
                                                       args["days"]))
        print("source     %s (%d page(s), %d treasury row(s), no block scan)"
              % (INDEX_HOST, pages, len(all_rows)))
        if paging_works is False:
            print("paging     the host ignores skip (page 2 is page 1) -> "
                  "this read is the NEWEST %d operations and no flag goes deeper"
                  % len(ops))
        if reached_back:
            print("floor      oldest row in this read: %s COT%s" % (
                reached_back, "" if covered else "  <-- WINDOW NOT FULLY COVERED"))
        print("")
        print_table(rows)
        print("")
        print("per COT day")
        for day, agg in summary["days"].items():
            print("  %-10s %2d payout(s)  %18s FJ" % (day, agg["count"], pretty_fj(agg["wei"])))
        print("")
        print("today %s  %d / %d paid payouts, %s FJ" % (
            summary["today"]["day"], summary["today"]["count"],
            PAID_CLAIMS_PER_COT_DAY, pretty_fj(summary["today"]["wei"])))
        print("tool rail  %d payout(s) of tool size in the covered part%s" % (
            len(tool_rows), "" if tool_rows else " -> no 15M/18M payment seen here"))
        if not covered:
            print("")
            print("INCOMPLETE: the index floor is %s COT, inside the window you asked for." % reached_back)
            print("            Payouts older than that are NOT in this read, and --pages cannot reach them.")
            print("            For the full window use: ledger or slots with a paid --rpc.")
            print("            exit 5 marks this read as partial on purpose.")
            return 5
    return 0


# ------------------------------------------------------------- reporting ----

def summarize(rows, today_cot, week_start_cot):
    by_day = {}
    for r in rows:
        by_day.setdefault(r["cot_day"], []).append(r)
    today_rows = by_day.get(today_cot, [])
    week_rows = [r for r in rows if r["cot_day"] >= week_start_cot]
    return {
        "days": {d.isoformat(): {"count": len(v), "wei": sum(r["wei"] for r in v)}
                 for d, v in sorted(by_day.items())},
        "today": {"day": today_cot.isoformat(), "count": len(today_rows),
                  "wei": sum(r["wei"] for r in today_rows),
                  "cap": PAID_CLAIMS_PER_COT_DAY},
        "week": {"start": week_start_cot.isoformat(), "count": len(week_rows),
                 "wei": sum(r["wei"] for r in week_rows)},
        "total": {"count": len(rows), "wei": sum(r["wei"] for r in rows)},
    }


def print_table(rows):
    if not rows:
        print("no treasury -> wallet payouts in this window")
        return
    print("%-10s %-17s %18s %11s  %-12s %s" % (
        "COT day", "time (COT)", "amount FJ", "block", "tx", "receipt / rail"))
    for r in rows:
        rail = ",".join(classify_amount(r["wei"])) or "amount matches no board rail"
        print("%-10s %-17s %18s %11s  %-12s %s | %s" % (
            r["cot_day"].isoformat(), r["cot"].strftime("%Y-%m-%d %H:%M"),
            wei_to_fj(r["wei"]), "{:,}".format(r["block"]) if r.get("block") else "index",
            r["tx"][:10] + "..",
            r.get("receipt_note", "not checked"), rail))


def ledger(args):
    wallet = parse_address(args["wallet"])
    days = args["days"]
    chain = Chain([args["rpc"]] if args.get("rpc") else None, jobs=args["jobs"])
    head = chain.head()
    now_ts = chain.block_ts(head)
    start_ts, end_ts, d0, d1 = window_bounds(now_ts, days)
    start_block = chain.block_at(start_ts)
    raw = chain.transfer_logs(start_block, head)
    rows = annotate_times(chain, build_rows(raw, wallet))

    mismatch = 0
    if not args["no_verify"]:
        receipts = Chain([args["rpc"]] if args.get("rpc") else None, jobs=args["jobs"])
        for r in rows:
            ok, why = verify_with_receipt(r, receipts.receipt(r["tx"]))
            r["receipt_note"] = "ok (%s)" % why if ok else "MISMATCH (%s)" % why
            if not ok:
                mismatch += 1

    today = cot_date(now_ts)
    summary = summarize(rows, today, cot_week_start(today))
    balance = chain.balance_of(wallet)
    ratio = hold_ratio(balance, summary["total"]["wei"])

    if args["json"]:
        print(json.dumps({
            "wallet": wallet, "head": head,
            "window": {"from": d0.isoformat(), "to": d1.isoformat(), "days": days,
                       "start_block": start_block, "end_block": head},
            "providers": chain.used,
            "payouts": [{"block": r["block"], "tx": r["tx"],
                         "wei": str(r["wei"]), "fj": wei_to_fj(r["wei"]),
                         "cot_time": r["cot"].isoformat(),
                         "cot_day": r["cot_day"].isoformat(),
                         "rails_amount_only": classify_amount(r["wei"]),
                         "receipt": r.get("receipt_note", "not checked")} for r in rows],
            "summary": summary,
            "hold": {"balance_wei": str(balance), "balance_fj": wei_to_fj(balance),
                     "received_wei": str(summary["total"]["wei"]),
                     "ratio": None if ratio is None else round(ratio, 6),
                     "threshold": HOLD_THRESHOLD,
                     "looks_like_1_2x": None if ratio is None else ratio >= HOLD_THRESHOLD},
            "exit": 4 if mismatch else 0,
        }, indent=2))
    else:
        print("wallet     %s" % wallet)
        print("window     %s .. %s COT (%.2f days)" % (d0.isoformat(), d1.isoformat(), days))
        print("blocks     %s .. %s (head %s)" % ("{:,}".format(start_block),
                                                 "{:,}".format(head), "{:,}".format(head)))
        print("providers  logs=%s receipts=%s" % (
            chain.used.get("default", "?"),
            "skipped (--no-verify)" if args["no_verify"] else "cross-read"))
        print("")
        print_table(rows)
        print("")
        print("per COT day")
        for day, agg in summary["days"].items():
            print("  %-10s %2d payout(s)  %18s FJ" % (day, agg["count"], pretty_fj(agg["wei"])))
        print("")
        print("caps (board: %d paid claims per COT day)" % PAID_CLAIMS_PER_COT_DAY)
        print("  today %s      %d / %d paid payouts, %s FJ" % (
            summary["today"]["day"], summary["today"]["count"], PAID_CLAIMS_PER_COT_DAY,
            pretty_fj(summary["today"]["wei"])))
        print("  week from %s  %d payout(s), %s FJ" % (
            summary["week"]["start"], summary["week"]["count"], pretty_fj(summary["week"]["wei"])))
        print("")
        print("hold check (board: >=80% of what we sent still held pays 1.2x)")
        print("  received in window  %s FJ" % wei_to_fj(summary["total"]["wei"]))
        print("  balance now         %s FJ" % wei_to_fj(balance))
        if ratio is None:
            print("  ratio               n/a (nothing received in this window)")
        else:
            print("  ratio (window)      %.1f%%  -> %s" % (
                ratio * 100, "looks like 1.2x" if ratio >= HOLD_THRESHOLD
                else "below 80%: 1.2x looks lost"))
        if mismatch:
            print("")
            print("VERIFICATION MISMATCH on %d payout(s): log and receipt disagree." % mismatch)
    return 4 if mismatch else 0


def hold(args):
    wallet = parse_address(args["wallet"])
    days = args["days"]
    chain = Chain([args["rpc"]] if args.get("rpc") else None, jobs=args["jobs"])
    head = chain.head()
    now_ts = chain.block_ts(head)
    start_ts, _, d0, d1 = window_bounds(now_ts, days)
    start_block = chain.block_at(start_ts)
    raw = chain.transfer_logs(start_block, head)
    rows = build_rows(raw, wallet)
    received = sum(r["wei"] for r in rows)
    balance = chain.balance_of(wallet)
    ratio = hold_ratio(balance, received)
    out = {"wallet": wallet, "head": head,
           "window": {"from": d0.isoformat(), "to": d1.isoformat(), "days": days},
           "received_wei": str(received), "received_fj": wei_to_fj(received),
           "balance_wei": str(balance), "balance_fj": wei_to_fj(balance),
           "ratio": None if ratio is None else round(ratio, 6),
           "threshold": HOLD_THRESHOLD,
           "looks_like_1_2x": None if ratio is None else ratio >= HOLD_THRESHOLD,
           "payouts": len(rows)}
    if args["json"]:
        print(json.dumps(out, indent=2))
    else:
        print("wallet      %s" % wallet)
        print("window      %s .. %s COT (%d day(s))" % (d0.isoformat(), d1.isoformat(), days))
        print("payouts     %d" % len(rows))
        print("received    %s FJ" % wei_to_fj(received))
        print("balance     %s FJ" % wei_to_fj(balance))
        print("ratio       %s" % ("n/a (nothing received in window)" if ratio is None
                                 else "%.1f%% -> %s" % (ratio * 100,
                                     "looks like 1.2x" if ratio >= HOLD_THRESHOLD
                                     else "below 80%: 1.2x looks lost")))
        print("note        the receipt-side check of the 1.2x rule is a balanceOf read")
        print("            at payout time; a windowed ratio is an estimate, not a ruling")
    return 0


def bucket_board(rows, week_start):
    """Group payouts by the rail(s) their amount is compatible with.

    A bucket is only 'unambiguous' when one rail explains the amount: the
    1.2x holder bonus makes 300k both a sip and a 1.2x reach, so that bucket
    is reported as ambiguous rather than assigned to whichever rail is nicer.
    """
    window, week = {}, {}
    for r in rows:
        rails = tuple(classify_amount(r["wei"])) or ("unclassified",)
        for store, active in ((window, True), (week, r["cot_day"] >= week_start)):
            if not active:
                continue
            b = store.setdefault(rails, {"count": 0, "wei": 0, "wallets": set()})
            b["count"] += 1
            b["wei"] += r["wei"]
            b["wallets"].add(r["to"])
    return window, week


def board(args):
    days = args["days"]
    chain = Chain([args["rpc"]] if args.get("rpc") else None, jobs=args["jobs"])
    head = chain.head()
    now_ts = chain.block_ts(head)
    start_ts, _, d0, d1 = window_bounds(now_ts, days)
    start_block = chain.block_at(start_ts)
    raw = chain.transfer_logs(start_block, head)          # every treasury outflow
    rows = annotate_times(chain, build_rows(raw, None))
    today = cot_date(now_ts)
    week_start = cot_week_start(today)
    window, week = bucket_board(rows, week_start)
    total = sum(r["wei"] for r in rows)

    def verdict(rails, used):
        if len(rails) != 1 or rails[0] == "unclassified":
            return "ambiguous" if rails[0] != "unclassified" else "no published rail"
        rail = rails[0]
        cap = SLOTS.get(rail)
        if cap is None:
            return "-"
        return "%d/%d used, %s" % (used, cap, "full" if used >= cap else "open")

    payload = {
        "head": head,
        "window": {"from": d0.isoformat(), "to": d1.isoformat(), "days": days,
                   "start_block": start_block, "end_block": head},
        "provider": chain.used.get("default"),
        "totals": {"payouts": len(rows), "wei": str(total), "fj": wei_to_fj(total),
                   "wallets": len({r["to"] for r in rows})},
        "week_start_cot": week_start.isoformat(),
        "buckets": [{"rails_amount_only": list(k), "count": v["count"],
                     "wei": str(v["wei"]), "fj": wei_to_fj(v["wei"]),
                     "week_count": (week.get(k) or {}).get("count", 0),
                     "verdict": verdict(k, (week.get(k) or {}).get("count", 0))}
                    for k, v in sorted(window.items(), key=lambda kv: -kv[1]["wei"])],
    }
    if args["json"]:
        print(json.dumps(payload, indent=2))
        return 0

    print("window     %s .. %s COT (%.2f days)" % (d0.isoformat(), d1.isoformat(), days))
    print("blocks     %s .. %s" % ("{:,}".format(start_block), "{:,}".format(head)))
    print("payouts    %d rows, %s FJ, %d wallet(s), provider %s" % (
        len(rows), pretty_fj(total), payload["totals"]["wallets"],
        chain.used.get("default", "?")))
    print("week       from %s 00:00 COT (board: weeks run Mon)" % week_start.isoformat())
    print("")
    print("%-34s %6s %14s %8s  %s" % ("rail (amount-only)", "count", "FJ", "week", "slot"))
    for b in payload["buckets"]:
        name = ",".join(b["rails_amount_only"])
        print("%-34s %6d %14s %8d  %s" % (name[:34], b["count"],
                                          pretty_fj(int(b["wei"])),
                                          b["week_count"], b["verdict"]))
    print("")
    print("amount alone cannot name a rail: the 1.2x holder bonus turns 250k into")
    print("300k and 15M into 18M. 'ambiguous' buckets are reported, not assigned.")
    return 0


def iso_to_ts(text):
    """ISO-8601 -> epoch seconds. Accepts Z, an explicit offset, or a trailing
    'COT' (the board's own clock); a bare time is read as UTC."""
    t = str(text).strip()
    cot = t.upper().endswith("COT")
    if cot:
        t = t[:-3].strip()
    t = t.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        raise ValueError("cannot read time %r (use 2026-10-26T00:00Z or "
                         "'2026-10-26 00:00 COT')" % text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc if not cot else COT)
    return int(dt.timestamp())


def slots(args):
    """Every treasury payout of at least --min FJ in a window, to anyone.

    The weekly rail question ("is the 15M tool slot already paid?") cannot be
    answered from one wallet's history: the winner is whoever the treasury paid,
    and that is a different address. So this scans the treasury's outbound
    transfers and names the rails by amount, and it prints the block range it
    covered so a short scan cannot pass for a full one.
    """
    min_wei = int(round(float(args["min"]) * 10 ** 18))
    if min_wei <= 0:
        raise ValueError("--min must be > 0 FJ")
    chain = Chain([args["rpc"]] if args.get("rpc") else None, jobs=args["jobs"])
    head = chain.head()
    now_ts = chain.block_ts(head)
    start_ts = iso_to_ts(args["from"]) if args.get("from") else now_ts - int(args["days"] * 86400)
    end_ts = min(iso_to_ts(args["to"]), now_ts) if args.get("to") else now_ts
    if start_ts >= end_ts:
        raise ValueError("--from must be earlier than --to")
    start_block = chain.block_at(start_ts)
    end_block = min(chain.block_at(end_ts), head)

    raw = chain.transfer_logs(start_block, end_block)
    everything = annotate_times(chain, build_rows(raw, None))
    rows = [r for r in everything if r["wei"] >= min_wei and start_ts <= r["ts"] <= end_ts]
    windows = (end_block - start_block) // WINDOW + 1
    big_tool = [r for r in rows if "tool" in classify_amount(r["wei"])]

    if args["json"]:
        print(json.dumps({
            "source": "eth_getLogs", "providers": chain.used,
            "window": {"from_cot": cot_datetime(start_ts).isoformat(),
                       "to_cot": cot_datetime(end_ts).isoformat(),
                       "start_block": start_block, "end_block": end_block,
                       "windows_scanned": windows},
            "treasury_outflows_seen": len(everything),
            "min_fj": str(args["min"]),
            "big_payouts": [{"tx": r["tx"], "block": r["block"], "to": r["to"],
                             "wei": str(r["wei"]), "fj": wei_to_fj(r["wei"]),
                             "cot_time": r["cot"].isoformat(),
                             "cot_day": r["cot_day"].isoformat(),
                             "rails_amount_only": classify_amount(r["wei"])} for r in rows],
            "tool_sized": len(big_tool),
        }, indent=2))
        return 0

    print("treasury   %s" % TREASURY)
    print("window     %s .. %s COT" % (cot_datetime(start_ts).strftime("%Y-%m-%d %H:%M"),
                                       cot_datetime(end_ts).strftime("%Y-%m-%d %H:%M")))
    print("blocks     %s .. %s" % ("{:,}".format(start_block), "{:,}".format(end_block)))
    print("scan       %d window(s) of %d blocks, every one answered (no silent gaps)"
          % (windows, WINDOW))
    print("outflows   %d treasury payout(s) in window, %d at or above %s FJ"
          % (len(everything), len(rows), args["min"]))
    print("")
    if rows:
        print("%-10s %-17s %16s %-38s %s" % ("COT day", "time (COT)", "amount FJ", "to", "tx"))
        for r in rows:
            print("%-10s %-17s %16s %-38s %s" % (
                r["cot_day"].isoformat(), r["cot"].strftime("%Y-%m-%d %H:%M"),
                wei_to_fj(r["wei"]), r["to"], r["tx"][:20] + ".."))
    else:
        print("no treasury payout reached %s FJ in this window - the big rails were "
              "not paid here" % args["min"])
    print("")
    print("tool rail  %d payment(s) of tool size (15M, or 18M at 1.2x) in this window%s"
          % (len(big_tool), "" if big_tool else " -> slot not paid in the part scanned"))
    print("note       amount alone cannot name a rail; 'tool' means 15M or 18M, "
          "which is also what meme x7.5 and dogma x5 would look like, so read the "
          "recipient before calling the slot taken.")
    return 0


# --------------------------------------------------------------- payouts ----
# The board publishes its own payout ledger on flapjax.surge.sh: every line has
# the amount, the COT day, the rail *by name*, the handle that was paid and the
# tx hash. A block scan can only tell you that 15,000,000 FJ left the treasury;
# it cannot tell you the treasury paid it as "tool" - and it cannot be made to
# cover a whole week on free endpoints (public nodes cap eth_getLogs at 5,000
# blocks, so a week is ~270 windows). One page fetch answers the weekly-slot
# question that the scan cannot.

PAYOUTS_URL = "https://flapjax.surge.sh/"
PAYOUTS_UA = "flapjax-calories/1.0 (read-only; one page fetch)"


def fetch_payouts_page(url=PAYOUTS_URL, timeout=45, rounds=3):
    """Fetch the board page. Raises ChainError when it never answers."""
    last = None
    for _ in range(max(1, int(rounds))):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": PAYOUTS_UA})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:                       # noqa: BLE001 - retried
            last = exc
            time.sleep(1.5)
    raise ChainError("board page unreachable: %s" % last)


_ENTITIES = (("&middot;", "·"), ("&hellip;", "…"), ("&nbsp;", " "),
             ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"))


def _text(fragment):
    """Tags out, entities in: the ledger writes its separators as
    &middot; in some builds and as a literal · in others; the rail
    regex must not care which."""
    out = re.sub(r"<[^>]+>", " ", fragment or "")
    for ent, ch in _ENTITIES:
        out = out.replace(ent, ch)
    return re.sub(r"\s+", " ", out).strip()


def parse_payouts(html):
    """Parse the board's payout ledger into rows.

    Each row: handle, amount_fj (float), day ('YYYY-MM-DD', COT), rail (the
    board's own label, lower-cased), rail_key (first word, for slot counting),
    tx (full hash or None), tx_short ('0x0fd8a2a4…a0ec'), work (the verifier
    sentence when the board printed one).
    """
    rows = []
    handle = None
    for m in re.finditer(
            r'<span class="fjx-c-handle">([^<]+)</span>'
            r'|<li class="fjx-c-item">(.*?)</li>', html, re.S):
        if m.group(1) is not None:
            handle = m.group(1).strip()
            continue
        block = m.group(2)
        amt = re.search(r'<span class="fjx-c-amt">\s*([\d,]+)\s*FLAPJAX', block)
        meta = re.search(r'<span class="fjx-c-meta">(.*?)</span>', block, re.S)
        if not amt or not meta:
            continue
        meta_txt = _text(meta.group(1))
        day = re.search(r"(\d{4}-\d{2}-\d{2})", meta_txt)
        if not day:
            continue
        rail = ""
        rail_m = re.search(r"\d{4}-\d{2}-\d{2}\s*·\s*(.*?)\s*·\s*(?:tx\b|$)",
                           meta_txt)
        if rail_m:
            rail = rail_m.group(1).strip().lower()
        full_tx = re.search(r"bscscan\.com/tx/(0x[0-9a-fA-F]{64})", block)
        short_tx = re.search(r"tx\s+(0x[0-9a-fA-F]{4,10})\s*…", meta_txt) \
            or re.search(r"tx\s+([0-9a-fA-F]{4,10})", meta_txt)
        work = re.search(r'<span class="fjx-c-work">(.*?)</span>', block, re.S)
        rows.append({
            "handle": handle,
            "amount_fj": float(amt.group(1).replace(",", "")),
            "day": day.group(1),
            "rail": rail,
            "rail_key": (rail.split(" ")[0] if rail else ""),
            "tx": full_tx.group(1) if full_tx else None,
            "tx_short": short_tx.group(1) if short_tx else None,
            "work": _text(work.group(1))[:200] if work else "",
        })
    return rows


def parse_treasury_stamp(html):
    """The page's own live receipt: (balance_fj, block, '2026-11-01 11:22 COT')."""
    m = re.search(r"([\d,]{4,})\s*FLAPJAX at BSC block\s*(\d+)\s*\(([^)]+)\)",
                  html)
    if not m:
        return None
    return (float(m.group(1).replace(",", "")), int(m.group(2)), m.group(3))


def slot_state(rows, rail, day):
    """Slots left on a weekly rail in the COT week containing `day`.

    Returns (cap, paid, open_). cap is None when the board prints no weekly cap
    for that rail; open_ is None when there is no cap to compare against.
    """
    cap = SLOTS.get(rail)
    wk = cot_week_start(day)
    paid = sum(1 for r in rows
               if r["rail_key"] == rail and r["day"]
               and wk <= date.fromisoformat(r["day"]) <= wk + timedelta(days=6))
    if cap is None:
        return (None, paid, None)
    return (cap, paid, paid < cap)


def payouts(args):
    """Answer 'is this week's <rail> slot still open' from the board's ledger."""
    html = fetch_payouts_page()
    rows = parse_payouts(html)
    if not rows:
        sys.stderr.write("board page fetched but no ledger rows parsed - the "
                         "markup changed; refusing to report an empty ledger\n")
        return 3
    today = cot_date(int(args.get("now") or time.time()))
    week = cot_week_start(today)
    since = args.get("since")
    rail = args.get("rail")
    keep = []
    for r in rows:
        if since and r["day"] < since:
            continue
        if rail and not (r["rail_key"] == rail or r["rail"].startswith(rail)):
            continue
        keep.append(r)
    keep.sort(key=lambda r: (r["day"], r["amount_fj"]), reverse=True)
    limit = max(1, int(args.get("limit") or 20))
    stamp = parse_treasury_stamp(html)
    verdicts = []
    for name, _base in RAILS:
        cap, paid, open_ = slot_state(rows, name, today)
        if cap is not None:
            verdicts.append({"rail": name, "cap": cap, "paid": paid,
                             "open": open_})
    if args["json"]:
        print(json.dumps({
            "source": {"url": PAYOUTS_URL, "rows": len(rows),
                       "treasury": ({"balance_fj": stamp[0], "block": stamp[1],
                                     "as_of": stamp[2]} if stamp else None)},
            "week_start_cot": week.isoformat(),
            "slots": verdicts,
            "payouts": keep[:limit],
        }, indent=2, sort_keys=True))
        return 0
    print("source %s (%d ledger row(s), one page, no block scan)"
          % (PAYOUTS_URL, len(rows)))
    if stamp:
        print("page receipt: %s FJ at block %d (%s)"
              % (pretty_fj(int(round(stamp[0] * 10 ** 18))), stamp[1], stamp[2]))
    print("COT week containing %s starts %s (Mon 00:00 COT)" % (today, week))
    print("")
    print("weekly slots, from the board's own ledger:")
    for v in verdicts:
        state = "OPEN" if v["open"] else "TAKEN"
        print("  %-6s %d/%d paid this week  -> %s"
              % (v["rail"], v["paid"], v["cap"], state))
    if keep:
        print("")
        shown = keep[:limit]
        print("%-10s %14s  %-10s %-18s %s"
              % ("day", "amount FJ", "rail", "handle", "tx"))
        for r in shown:
            print("%-10s %14s  %-10s %-18s %s"
                  % (r["day"], pretty_fj(int(round(r["amount_fj"] * 10 ** 18))),
                     r["rail"] or "?", (r["handle"] or "?")[:18],
                     r["tx_short"] or "?"))
        if len(keep) > len(shown):
            print("... %d more row(s) not shown (--limit %d)"
                  % (len(keep) - len(shown), limit))
    else:
        print("")
        print("no ledger row matched %s" % (
            " ".join(x for x in ("--rail " + rail if rail else "",
                                 "--since " + since if since else "") if x)
            or "this filter"))
    return 0


USAGE = """flapjax-calories - read-only FLAPJAX income ledger for one wallet

  python3 flapjax_calories.py ledger <wallet> [--days 7] [--json] [--no-verify] [--rpc URL] [--jobs N]
  python3 flapjax_calories.py hold   <wallet> [--days 30] [--json] [--rpc URL] [--jobs N]
  python3 flapjax_calories.py board  [--days 7] [--json] [--rpc URL] [--jobs N]
  python3 flapjax_calories.py index  <wallet> [--days 7] [--pages 3] [--json]
  python3 flapjax_calories.py slots  [--days 7] [--from T] [--to T] [--min FJ] [--json] [--rpc URL] [--jobs N]
  python3 flapjax_calories.py payouts [--rail NAME] [--since YYYY-MM-DD] [--limit N] [--json]

ledger  treasury -> wallet payouts in the last N COT days, each one cross-read
        against its own receipt, grouped by COT day, with the daily paid-claim
        count and the 80%-hold ratio.
hold    received vs still-held, the number behind the 1.2x holder bonus.
board   every treasury payout in the window, bucketed by the rail its amount is
        compatible with, against the board's weekly slot caps - so you can see
        whether the tool slot (1/week) is still open before you spend a day on it.
index   the same treasury ledger from a public token index instead of a block
        scan: one request, no throttled eth_getLogs, no block numbers. The host
        returns the newest 100 operations and ignores skip, so this command
        probes that and prints the floor it actually reached; when the window it
        was asked for is not covered it says so and exits 5. It never calls a
        partial window complete.
slots   treasury payouts of at least --min FJ to anyone, which is how you answer
        the weekly rail question: a slot is taken when the treasury paid it, not
        when your own wallet received something. Prints the block range scanned.
        --from/--to take ISO times ('2026-10-26T00:00Z' or '2026-10-26 00:00 COT').
payouts the board's own payout ledger (flapjax.surge.sh), one page: amount,
        COT day, the rail *by name*, the handle paid and the tx hash - plus
        its weekly-slot verdict per rail (tool 1/week etc). A block scan can
        prove 15,000,000 FJ left the treasury but not that it was paid as
        'tool'; and a whole week is ~270 throttled windows on free nodes.
        --rail tool --since 2026-10-26 answers 'is this week's tool slot
        still open' in one request.

Exit codes: 0 ok, 2 bad input, 3 no provider, 4 log and receipt disagree,
            5 index window not fully covered.
"""


def parse_argv(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        raise SystemExit(USAGE)
    cmd, rest = argv[0], argv[1:]
    if cmd not in ("ledger", "hold", "board", "index", "slots", "payouts"):
        raise SystemExit("unknown command %r\n\n%s" % (cmd, USAGE))
    walletless = ("board", "slots", "payouts")
    if cmd not in walletless and not rest:
        raise SystemExit("missing wallet address\n\n%s" % USAGE)
    args = {"cmd": cmd, "wallet": None if cmd in walletless else rest[0],
            "days": 30.0 if cmd == "hold" else 7.0,
            "json": False, "no_verify": False, "rpc": None, "jobs": 3, "pages": 3,
            "from": None, "to": None, "min": 1000000.0,
            "rail": None, "since": None, "limit": 20, "now": None}
    i = 0 if cmd in walletless else 1
    while i < len(rest):
        a = rest[i]
        if a == "--json":
            args["json"] = True
        elif a == "--no-verify":
            args["no_verify"] = True
        elif a in ("--days", "--rpc", "--jobs", "--pages", "--from", "--to",
                   "--min", "--rail", "--since", "--limit"):
            if i + 1 >= len(rest):
                raise SystemExit("%s needs a value" % a)
            if a == "--days":
                args["days"] = float(rest[i + 1])
                if args["days"] <= 0:
                    raise SystemExit("--days must be > 0")
            elif a == "--pages":
                args["pages"] = max(1, int(rest[i + 1]))
            elif a == "--jobs":
                args["jobs"] = max(1, int(rest[i + 1]))
            elif a == "--min":
                args["min"] = float(rest[i + 1])
            elif a == "--limit":
                args["limit"] = max(1, int(rest[i + 1]))
            elif a == "--rail":
                args["rail"] = rest[i + 1].strip().lower()
            elif a == "--since":
                try:
                    date.fromisoformat(rest[i + 1])
                except ValueError:
                    raise SystemExit("--since needs a YYYY-MM-DD day")
                args["since"] = rest[i + 1]
            elif a in ("--from", "--to"):
                args[a[2:]] = rest[i + 1]
            else:
                args["rpc"] = rest[i + 1]
            i += 1
        else:
            raise SystemExit("unknown option %r\n\n%s" % (a, USAGE))
        i += 1
    return args


def main(argv=None):
    args = parse_argv(list(sys.argv[1:] if argv is None else argv))
    try:
        if args["cmd"] == "ledger":
            return ledger(args)
        if args["cmd"] == "hold":
            return hold(args)
        if args["cmd"] == "index":
            return index_cmd(args)
        if args["cmd"] == "slots":
            return slots(args)
        if args["cmd"] == "payouts":
            return payouts(args)
        return board(args)
    except ValueError as exc:
        sys.stderr.write("bad input: %s\n" % exc)
        return 2
    except ChainError as exc:
        sys.stderr.write("no provider answered: %s\n" % exc)
        return 3


if __name__ == "__main__":
    sys.exit(main())
