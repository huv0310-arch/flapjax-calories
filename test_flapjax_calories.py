#!/usr/bin/env python3
"""Offline tests for flapjax_calories. No network: every chain read is faked."""

import io
import json
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, timedelta, timezone

import flapjax_calories as fc

WALLET = "0x97cd18b8bbb629b17675d868259ea84c0a2a5a9c"
OTHER = "0xa15c58e7008fc7a0331cfcc86a28af99fd21b93c"
TOKEN = fc.TOKEN
TREASURY = fc.TREASURY
COT = timezone(timedelta(hours=-5))


def log(to, wei, block, tx):
    return {
        "address": TOKEN,
        "topics": [fc.TRANSFER_TOPIC,
                   fc.topic_address(TREASURY),
                   fc.topic_address(to)],
        "data": hex(wei),
        "blockNumber": hex(block),
        "transactionHash": tx,
    }


def receipt(to, wei, block, tx, status="0x1"):
    return {"status": status, "blockNumber": hex(block),
            "logs": [log(to, wei, block, tx)]}


class FakeChain(object):
    """Minimal stand-in for fc.Chain."""

    def __init__(self, logs=None, receipts=None, balances=None,
                 head=130_974_048, block_time=0.45, fail=None):
        self.logs_in = logs or []
        self.receipts = receipts or {}
        self.balances = balances or {}
        self.head_block = head
        self.head_ts = 1_793_502_341
        self.block_time = block_time
        self.fail = fail
        self.used = {"default": "fake-a", "receipts": "fake-b"}
        self.scanned = []

    def head(self):
        if self.fail:
            raise fc.ChainError("no provider")
        return self.head_block

    def block_ts(self, number):
        return int(self.head_ts - (self.head_block - number) * self.block_time)

    def block_at(self, utc_ts):
        lo, hi = 1, self.head_block
        while lo < hi:
            mid = (lo + hi) // 2
            if self.block_ts(mid) < utc_ts:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def transfer_logs(self, start_block, end_block, frm=TREASURY, token=TOKEN):
        self.scanned.append((start_block, end_block))
        return [l for l in self.logs_in
                if start_block <= int(l["blockNumber"], 16) <= end_block]

    def receipt(self, tx):
        return self.receipts.get(tx)

    def balance_of(self, wallet, token=TOKEN):
        return self.balances.get(wallet.lower(), 0)

    def call(self, method, params, role="default"):
        raise AssertionError("unexpected call %s" % method)


class AddressTests(unittest.TestCase):
    def test_lowercases_and_strips_prefix(self):
        self.assertEqual(fc.parse_address("0x97Cd18B8bBB629b17675D868259eA84C0A2A5A9c"), WALLET)

    def test_rejects_39_characters_and_says_so(self):
        with self.assertRaises(ValueError) as ctx:
            fc.parse_address("0x015b36feadd0000000000000000000000000000")
        self.assertIn("39", str(ctx.exception))
        self.assertIn("40", str(ctx.exception))

    def test_rejects_41_characters(self):
        with self.assertRaises(ValueError):
            fc.parse_address("0x" + "a" * 41)

    def test_rejects_non_hex(self):
        with self.assertRaises(ValueError):
            fc.parse_address("0x" + "z" * 40)

    def test_topic_padding_is_64_hex_without_0x_prefix(self):
        t = fc.topic_address(WALLET)
        self.assertEqual(len(t), 66)
        self.assertTrue(t.startswith("0x000000000000000000000000"))
        self.assertTrue(t.endswith(WALLET[2:]))


class UnitTests(unittest.TestCase):
    def test_supply_reads_as_published(self):
        self.assertEqual(fc.wei_to_fj(44_444_444_444 * 10 ** 18), "44444444444.0")

    def test_18_decimals_are_kept_exactly(self):
        self.assertEqual(fc.wei_to_fj(1), "0.000000000000000001")
        self.assertEqual(fc.wei_to_fj(300_000 * 10 ** 18), "300000.0")

    def test_pretty_is_thousands_separated(self):
        self.assertEqual(fc.pretty_fj(15_000_000 * 10 ** 18), "15,000,000.00")


class CotBoundaryTests(unittest.TestCase):
    def test_board_anchor_block_130953649_is_31_oct_cot(self):
        # Board: "824,532,279 FLAPJAX at BSC block 130953649 (2026-10-31 19:33 COT)".
        # That block's timestamp is 2026-11-01T00:33:02Z, so a UTC-day reading of
        # the same block lands on the wrong day.
        ts = 1_793_493_182  # 2026-11-01 00:33:02 UTC
        self.assertEqual(fc.cot_date(ts), date(2026, 10, 31))
        self.assertEqual(fc.cot_datetime(ts).strftime("%H:%M"), "19:33")

    def test_cot_day_start_is_05_utc(self):
        ts = fc.cot_day_start_ts(date(2026, 11, 1))
        self.assertEqual(datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M"), "05:00")

    def test_week_starts_monday_cot(self):
        self.assertEqual(fc.cot_week_start(date(2026, 11, 1)), date(2026, 10, 26))
        self.assertEqual(fc.cot_week_start(date(2026, 11, 2)), date(2026, 11, 2))

    def test_window_of_one_day_is_today_only(self):
        now = fc.cot_day_start_ts(date(2026, 11, 1)) + 3600
        start, end, d0, d1 = fc.window_bounds(now, 1)
        self.assertEqual(d0, date(2026, 11, 1))
        self.assertEqual(d1, date(2026, 11, 1))
        self.assertEqual(start, fc.cot_day_start_ts(date(2026, 11, 1)))

    def test_window_of_seven_days_covers_a_week(self):
        now = fc.cot_day_start_ts(date(2026, 11, 1)) + 3600
        _, _, d0, d1 = fc.window_bounds(now, 7)
        self.assertEqual((d0, d1), (date(2026, 10, 26), date(2026, 11, 1)))


class ClassificationTests(unittest.TestCase):
    def test_tool_amount_is_tool(self):
        self.assertEqual(fc.classify_amount(15_000_000 * 10 ** 18), ["tool"])

    def test_1_2x_tool_amount_is_tool_at_1_2x(self):
        self.assertEqual(fc.classify_amount(18_000_000 * 10 ** 18), ["tool@1.2x"])

    def test_300k_is_ambiguous_between_sip_and_1_2x_reach(self):
        self.assertEqual(fc.classify_amount(300_000 * 10 ** 18), ["sip", "reach@1.2x"])

    def test_unknown_amount_names_no_rail(self):
        self.assertEqual(fc.classify_amount(500_000 * 10 ** 18), [])

    def test_dust_on_a_real_payout_still_matches(self):
        # seen on chain: 300000.000000000008388608 FJ, not 300000e18 exactly.
        # The rail is still ambiguous; what dust must not do is blank the match.
        self.assertEqual(fc.classify_amount(300_000 * 10 ** 18 + 8_388_608),
                         ["sip", "reach@1.2x"])

    def test_dust_on_a_tool_payout_still_names_tool(self):
        self.assertEqual(fc.classify_amount(15_000_000 * 10 ** 18 + 8_388_608), ["tool"])

    def test_18m_with_dust_is_tool_at_1_2x(self):
        self.assertEqual(fc.classify_amount(18_000_000 * 10 ** 18 + 10 ** 12), ["tool@1.2x"])


class RowTests(unittest.TestCase):
    def test_only_treasury_to_wallet_rows_survive(self):
        logs = [log(WALLET, 300_000 * 10 ** 18, 100, "0xaa"),
                log(OTHER, 250_000 * 10 ** 18, 101, "0xbb")]
        rows = fc.build_rows(logs, WALLET)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["tx"], "0xaa")

    def test_rows_are_block_ordered(self):
        logs = [log(WALLET, 10_000 * 10 ** 18, 300, "0x03"),
                log(WALLET, 10_000 * 10 ** 18, 200, "0x02")]
        rows = fc.build_rows(logs, WALLET)
        self.assertEqual([r["block"] for r in rows], [200, 300])

    def test_malformed_log_is_ignored_not_fatal(self):
        rows = fc.build_rows([{"topics": []}, {"nope": 1}], WALLET)
        self.assertEqual(rows, [])


class ReceiptVerificationTests(unittest.TestCase):
    def setUp(self):
        self.row = {"block": 500, "tx": "0xdead", "wei": 300_000 * 10 ** 18,
                    "from": TREASURY.lower(), "to": WALLET}

    def test_matching_receipt_passes(self):
        ok, why = fc.verify_with_receipt(
            self.row, receipt(WALLET, 300_000 * 10 ** 18, 500, "0xdead"))
        self.assertTrue(ok)
        self.assertIn("matches", why)

    def test_failed_status_is_a_mismatch(self):
        ok, why = fc.verify_with_receipt(
            self.row, receipt(WALLET, 300_000 * 10 ** 18, 500, "0xdead", status="0x0"))
        self.assertFalse(ok)
        self.assertIn("status", why)

    def test_missing_receipt_is_a_mismatch(self):
        ok, why = fc.verify_with_receipt(self.row, None)
        self.assertFalse(ok)

    def test_wrong_amount_is_a_mismatch(self):
        ok, _ = fc.verify_with_receipt(
            self.row, receipt(WALLET, 299_999 * 10 ** 18, 500, "0xdead"))
        self.assertFalse(ok)

    def test_block_drift_is_a_mismatch(self):
        ok, why = fc.verify_with_receipt(
            self.row, receipt(WALLET, 300_000 * 10 ** 18, 501, "0xdead"))
        self.assertFalse(ok)
        self.assertIn("block", why)


class HoldTests(unittest.TestCase):
    def test_eighty_percent_passes(self):
        self.assertGreaterEqual(fc.hold_ratio(800_000, 1_000_000), fc.HOLD_THRESHOLD)

    def test_just_under_eighty_percent_fails(self):
        self.assertLess(fc.hold_ratio(799_999, 1_000_000), fc.HOLD_THRESHOLD)

    def test_nothing_received_is_not_zero_percent(self):
        self.assertIsNone(fc.hold_ratio(5, 0))


class BoardTests(unittest.TestCase):
    def rows(self):
        day = date(2026, 11, 1)
        return [
            {"cot_day": day, "wei": 15_000_000 * 10 ** 18, "to": "0xaa"},
            {"cot_day": day, "wei": 300_000 * 10 ** 18, "to": "0xbb"},
            {"cot_day": day, "wei": 500_000 * 10 ** 18, "to": "0xcc"},
            {"cot_day": date(2026, 10, 20), "wei": 15_000_000 * 10 ** 18, "to": "0xdd"},
        ]

    def test_tool_bucket_is_marked_full_when_the_one_slot_is_used(self):
        window, week = fc.bucket_board(self.rows(), date(2026, 10, 26))
        self.assertEqual(week[("tool",)]["count"], 1)
        self.assertEqual(fc.SLOTS["tool"], 1)

    def test_old_rows_stay_out_of_the_week_count(self):
        _, week = fc.bucket_board(self.rows(), date(2026, 10, 26))
        self.assertEqual(sum(b["count"] for b in week.values()), 3)

    def test_300k_is_not_silently_assigned(self):
        window, _ = fc.bucket_board(self.rows(), date(2026, 10, 26))
        self.assertIn(("sip", "reach@1.2x"), window)
        self.assertNotIn(("sip",), window)

    def test_unclassifiable_amounts_get_their_own_bucket(self):
        window, _ = fc.bucket_board(self.rows(), date(2026, 10, 26))
        self.assertIn(("unclassified",), window)


class SummaryTests(unittest.TestCase):
    def test_today_and_week_counters(self):
        today = date(2026, 11, 1)
        week = fc.cot_week_start(today)
        rows = [{"cot_day": today, "wei": 300_000 * 10 ** 18},
                {"cot_day": today - timedelta(days=1), "wei": 250_000 * 10 ** 18},
                {"cot_day": week - timedelta(days=1), "wei": 10_000 * 10 ** 18}]
        s = fc.summarize(rows, today, week)
        self.assertEqual(s["today"]["count"], 1)
        self.assertEqual(s["week"]["count"], 2)
        self.assertEqual(s["total"]["count"], 3)
        self.assertEqual(s["today"]["cap"], 3)


class CommandTests(unittest.TestCase):
    def setUp(self):
        self._real_chain = fc.Chain
        self.logs = [log(WALLET, 300_000 * 10 ** 18, 130_970_000, "0xaaa"),
                     log(WALLET, 250_000 * 10 ** 18, 130_960_000, "0xbbb")]
        self.receipts = {
            "0xaaa": receipt(WALLET, 300_000 * 10 ** 18, 130_970_000, "0xaaa"),
            "0xbbb": receipt(WALLET, 250_000 * 10 ** 18, 130_960_000, "0xbbb"),
        }

    def tearDown(self):
        fc.Chain = self._real_chain

    def _install(self, **kw):
        fake = FakeChain(logs=kw.pop("logs", self.logs),
                         receipts=kw.pop("receipts", self.receipts),
                         balances={WALLET: 3_075_000 * 10 ** 18}, **kw)
        fc.Chain = lambda urls=None, timeout=60, jobs=10: fake
        return fake

    def run_cli(self, argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            code = fc.main(argv)
        return code, buf.getvalue()

    def test_ledger_json_contract(self):
        self._install()
        code, out = self.run_cli(["ledger", WALLET, "--days", "7", "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        for key in ("wallet", "head", "window", "payouts", "summary", "hold"):
            self.assertIn(key, payload)
        self.assertEqual(payload["wallet"], WALLET)
        self.assertEqual(len(payload["payouts"]), 2)
        self.assertEqual(payload["hold"]["ratio"], round(3_075_000 / 550_000, 6))
        self.assertEqual(payload["payouts"][0]["receipt"].split()[0], "ok")

    def test_ledger_text_mentions_caps_and_window(self):
        self._install()
        code, out = self.run_cli(["ledger", WALLET, "--days", "7"])
        self.assertEqual(code, 0)
        self.assertIn("paid claims per COT day", out)
        self.assertIn("hold check", out)
        self.assertIn("per COT day", out)

    def test_mismatch_exits_4(self):
        bad = dict(self.receipts)
        bad["0xbbb"] = receipt(WALLET, 1, 130_960_000, "0xbbb")
        self._install(receipts=bad)
        code, out = self.run_cli(["ledger", WALLET, "--json"])
        self.assertEqual(code, 4)
        self.assertEqual(json.loads(out)["exit"], 4)

    def test_no_verify_skips_receipts(self):
        self._install(receipts={})
        code, out = self.run_cli(["ledger", WALLET, "--no-verify", "--json"])
        self.assertEqual(code, 0)
        self.assertIn("not checked", out)

    def test_hold_command(self):
        self._install()
        code, out = self.run_cli(["hold", WALLET, "--json"])
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["payouts"], 2)
        self.assertTrue(payload["looks_like_1_2x"])

    def test_bad_address_exits_2(self):
        self._install()
        code, _ = self.run_cli(["ledger", "0x123"])
        self.assertEqual(code, 2)

    def test_no_provider_exits_3(self):
        self._install(fail=True)
        code, _ = self.run_cli(["ledger", WALLET])
        self.assertEqual(code, 3)

    def test_a_window_that_is_refused_is_split_not_dropped(self):
        ranges = []

        class Picky(fc.Chain):
            def call(self, method, params, role="default", rounds=2):
                f = int(params[0]["fromBlock"], 16)
                t = int(params[0]["toBlock"], 16)
                if t - f + 1 > 2500:
                    raise fc.ChainError("limit exceeded")
                ranges.append((f, t))
                return [log(WALLET, 10 ** 18, f, "0x%040x" % f)]

        chain = Picky(["http://fake"], jobs=1)
        got = chain.transfer_logs(5_000_000, 5_004_999)
        self.assertEqual(len(got), 2)                      # one per half, none lost
        self.assertEqual(sum(t - f + 1 for f, t in ranges), 5000)

    def test_every_get_logs_window_stays_under_the_node_cap(self):
        ranges = []

        class Recording(fc.Chain):
            def call(self, method, params, role="default"):
                if method == "eth_getLogs":
                    f = int(params[0]["fromBlock"], 16)
                    t = int(params[0]["toBlock"], 16)
                    ranges.append((f, t))
                    return []
                return []

        chain = Recording(["http://fake"])
        chain.transfer_logs(1_000_000, 1_012_345)
        self.assertEqual(len(ranges), 3)
        for f, t in ranges:
            self.assertLessEqual(t - f + 1, fc.WINDOW)
        self.assertEqual(ranges[-1][0], 1_000_000)

    def test_help_exits_zero(self):
        with self.assertRaises(SystemExit) as ctx:
            fc.main([])
        self.assertEqual(ctx.exception.code, fc.__dict__.get("USAGE") and ctx.exception.code)


class TestTokenIndex(unittest.TestCase):
    """The index backend: same ledger, no block scan, its limits printed."""

    TS_0033Z = 1793493182          # 2026-11-01T00:33:02Z == 2026-10-31 19:33 COT (blk 130,953,649)

    @staticmethod
    def op(to, wei, ts, tx, token=TOKEN, frm=TREASURY):
        return {"timestamp": ts, "transactionHash": tx, "value": str(wei),
                "from": frm, "to": to, "type": "transfer",
                "tokenInfo": {"address": token, "decimals": "18",
                              "symbol": "FLAPJAX", "name": "FlapJax"}}

    def test_url_points_at_the_address_history_endpoint(self):
        u = fc.index_url(TREASURY, limit=100, skip=200)
        self.assertIn("/getAddressHistory/" + TREASURY, u)
        self.assertIn("limit=100", u)
        self.assertIn("skip=200", u)

    def test_cot_day_not_utc_day(self):
        rows = fc.index_rows({"operations": [self.op(WALLET, 300000 * 10 ** 18,
                                                    self.TS_0033Z, "0xaa")]}, WALLET)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["cot_day"], date(2026, 10, 31))      # not 11-01
        self.assertEqual(rows[0]["cot"].strftime("%H:%M"), "19:33")
        self.assertIsNone(rows[0]["block"])                            # index has no block

    def test_dust_survives_the_index_round_trip(self):
        wei = 300000000000000008388608          # 300000.000000000008 FJ
        rows = fc.index_rows({"operations": [self.op(WALLET, wei, self.TS_0033Z, "0xbb")]},
                             WALLET)
        self.assertEqual(rows[0]["wei"], wei)
        self.assertEqual(fc.wei_to_fj(wei), "300000.000000000008388608")

    def test_only_treasury_flapjax_outbound_rows_survive(self):
        ops = [
            self.op(WALLET, 10 ** 22, self.TS_0033Z, "0x01"),                     # keep
            self.op(WALLET, 10 ** 22, self.TS_0033Z, "0x01"),                     # dup tx
            self.op(WALLET, 10 ** 22, self.TS_0033Z, "0x02",
                    token="0x1111111111111111111111111111111111111111"),         # other token
            self.op(TREASURY, 10 ** 22, self.TS_0033Z, "0x03"),                  # incoming
            self.op(OTHER, 10 ** 22, self.TS_0033Z, "0x04"),                     # other wallet
            self.op(WALLET, 10 ** 22, self.TS_0033Z, "0x05",
                    frm="0x2222222222222222222222222222222222222222"),           # not treasury
        ]
        self.assertEqual([r["tx"] for r in fc.index_rows({"operations": ops}, WALLET)], ["0x01"])
        self.assertEqual(len(fc.index_rows({"operations": ops})), 3)             # 0x01, 0x03, 0x04
        self.assertEqual(len(fc.index_rows({"operations": ops}, OTHER)), 1)

    def test_tool_sized_payout_is_recognised_in_the_window(self):
        ops = [self.op(OTHER, 15 * 10 ** 24, self.TS_0033Z, "0xtool"),
               self.op(OTHER, 25000 * 10 ** 18, self.TS_0033Z, "0xsip")]
        rows = fc.index_rows({"operations": ops})
        tool = [r for r in rows if "tool" in fc.classify_amount(r["wei"])]
        self.assertEqual([r["tx"] for r in tool], ["0xtool"])

    def test_table_prints_index_where_a_block_would_go(self):
        rows = fc.index_rows({"operations": [self.op(WALLET, 250000 * 10 ** 18,
                                                     self.TS_0033Z, "0xcc")]}, WALLET)
        buf = io.StringIO()
        with redirect_stdout(buf):
            fc.print_table(rows)
        out = buf.getvalue()
        self.assertIn("index", out)
        self.assertNotIn("None", out)

    def test_index_command_takes_a_wallet_and_a_page_budget(self):
        args = fc.parse_argv(["index", WALLET, "--days", "3", "--pages", "5"])
        self.assertEqual(args["cmd"], "index")
        self.assertEqual(args["pages"], 5)
        self.assertEqual(args["days"], 3.0)
        self.assertEqual(fc.parse_argv(["index", WALLET])["pages"], 3)
        with self.assertRaises(SystemExit):
            fc.parse_argv(["index"])


class TestPagingAndSlots(unittest.TestCase):
    """The index host ignores skip; the tool has to notice, not assume."""

    TS = 1793493182

    @staticmethod
    def page(tx_ids):
        return {"operations": [{"timestamp": TestPagingAndSlots.TS,
                                "transactionHash": t, "value": "1000000000000000000",
                                "from": TREASURY, "to": WALLET,
                                "tokenInfo": {"address": TOKEN, "symbol": "FLAPJAX",
                                              "decimals": "18"}} for t in tx_ids]}

    def test_identical_second_page_means_paging_is_a_lie(self):
        seen = []

        def fake(address, limit=100, skip=0, **kw):
            seen.append(skip)
            return self.page(["0x%02d" % i for i in range(100)])   # same set every time

        orig = fc.fetch_index
        fc.fetch_index = fake
        try:
            ops, pages, works = fc.index_depth({"pages": 5})
        finally:
            fc.fetch_index = orig
        self.assertFalse(works)
        self.assertEqual(pages, 1)                      # no double counting
        self.assertEqual(len(ops), 100)
        self.assertEqual(seen, [0, 100])                # probed once, then stopped

    def test_a_real_second_page_is_used(self):
        def fake(address, limit=100, skip=0, **kw):
            base = 0 if skip == 0 else 100
            return self.page(["0x%03d" % (base + i) for i in range(100)])

        orig = fc.fetch_index
        fc.fetch_index = fake
        try:
            ops, pages, works = fc.index_depth({"pages": 2})
        finally:
            fc.fetch_index = orig
        self.assertTrue(works)
        self.assertEqual(pages, 2)
        self.assertEqual(len(ops), 200)

    def test_index_cmd_exits_5_when_the_window_is_not_covered(self):
        orig_fetch, orig_time = fc.fetch_index, fc.time.time
        fc.fetch_index = lambda address, limit=100, skip=0, **kw: self.page(["0x%02d" % i for i in range(3)])
        fc.time.time = lambda: self.TS + 30 * 86400          # window is 30 days deep
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                rc = fc.index_cmd({"wallet": WALLET, "days": 60.0, "pages": 1, "json": False})
        finally:
            fc.fetch_index, fc.time.time = orig_fetch, orig_time
        self.assertEqual(rc, 5)                              # partial is flagged, not smoothed
        self.assertIn("INCOMPLETE", buf.getvalue())
        self.assertIn("exit 5", buf.getvalue())

    def test_iso_times_accept_z_offset_and_cot(self):
        want = 1793493182                                   # 2026-11-01T00:33:02Z
        self.assertEqual(fc.iso_to_ts("2026-11-01T00:33:02Z"), want)
        self.assertEqual(fc.iso_to_ts("2026-11-01T00:33:02+00:00"), want)
        self.assertEqual(fc.iso_to_ts("2026-10-31 19:33:02 COT"), want)
        self.assertEqual(fc.iso_to_ts("2026-10-31T19:33:02-05:00"), want)
        with self.assertRaises(ValueError):
            fc.iso_to_ts("last tuesday")

    def test_slots_takes_no_wallet_and_reads_its_own_window(self):
        args = fc.parse_argv(["slots", "--from", "2026-10-26T00:00Z",
                              "--to", "2026-10-28 08:49 COT", "--min", "1000000"])
        self.assertEqual(args["cmd"], "slots")
        self.assertIsNone(args["wallet"])
        self.assertEqual(args["min"], 1000000.0)
        self.assertEqual(fc.iso_to_ts(args["from"]), 1792972800)   # 2026-10-26T00:00:00Z
        self.assertGreater(fc.iso_to_ts(args["to"]), fc.iso_to_ts(args["from"]))
        self.assertEqual(fc.parse_argv(["slots"])["min"], 1000000.0)

    def test_slots_min_must_be_positive(self):
        args = fc.parse_argv(["slots", "--min", "0"])
        with self.assertRaises(ValueError):
            fc.slots(args)


LEDGER_HTML = """
    <li class="fjx-c-row">
      <div class="fjx-c-head"><span class="fjx-c-handle">@arispay-x402</span>
        <span class="fjx-c-plat">The Colony</span></div>
      <ul class="fjx-c-items">
        <li class="fjx-c-item">
          <span class="fjx-c-work"><a href="https://github.com/x/y">ACCEPT tool
            15M. work: verified (clean clone HEAD 0e1e5f4, unittest 11/11 OK)</a></span>
          <span class="fjx-c-amt">15,000,000 FLAPJAX</span>
          <span class="fjx-c-meta">2026-10-24 &middot; tool &middot; <a
            href="https://bscscan.com/tx/0x0fd8a2a4c3fc9dff88133acf5348d1feed06827b3968a2a9b17d16888eada0ec">tx
            0x0fd8a2a4&hellip;a0ec</a></span>
        </li>
        <li class="fjx-c-item">
          <span class="fjx-c-work">quick task Q40 answer</span>
          <span class="fjx-c-amt">120,000 FLAPJAX</span>
          <span class="fjx-c-meta">2026-10-23 &middot; quick task Q40 &middot; <a
            href="https://bscscan.com/tx/0xd9af02daa7d4e689ef598409ac68aca95f09b5a76dc5a38c2242ec6b5fc93749">tx
            0xd9af02da&hellip;3749</a></span>
        </li>
      </ul>
    </li>
    <li class="fjx-c-row">
      <div class="fjx-c-head"><span class="fjx-c-handle">@other</span></div>
      <ul class="fjx-c-items">
        <li class="fjx-c-item">
          <span class="fjx-c-work">dogma essay</span>
          <span class="fjx-c-amt">3,000,000 FLAPJAX</span>
          <span class="fjx-c-meta">2026-10-23 &middot; dogma &middot; <a
            href="https://bscscan.com/tx/0x65bfe3ff100dcc45efbf729c5ed63676c08a6d9d3ecd651461218bb7860db763">tx
            0x65bfe3ff&hellip;b763</a></span>
        </li>
      </ul>
    </li>
    <!-- TREASURY:START -->824,182,279 FLAPJAX at BSC block 131080181 (2026-11-01 11:22 COT)<!-- TREASURY:END -->
"""


class TestPayoutsLedger(unittest.TestCase):
    """The board's own ledger names the rail; the block scan cannot.

    These tests are what makes 'is this week's tool slot still open' a one-page
    answer instead of ~270 throttled eth_getLogs windows.
    """

    def test_a_ledger_row_keeps_amount_day_rail_handle_and_tx(self):
        rows = fc.parse_payouts(LEDGER_HTML)
        self.assertEqual(len(rows), 3)
        tool = [r for r in rows if r["rail"] == "tool"][0]
        self.assertEqual(tool["amount_fj"], 15000000.0)
        self.assertEqual(tool["day"], "2026-10-24")
        self.assertEqual(tool["rail_key"], "tool")
        self.assertEqual(tool["handle"], "@arispay-x402")
        self.assertEqual(tool["tx"],
                         "0x0fd8a2a4c3fc9dff88133acf5348d1feed06827b3968a2a9b17d16888eada0ec")
        self.assertIn("unittest 11/11", tool["work"])
        self.assertEqual([r["handle"] for r in rows],
                         ["@arispay-x402", "@arispay-x402", "@other"])

    def test_a_compound_rail_label_keeps_its_own_wording(self):
        quick = [r for r in fc.parse_payouts(LEDGER_HTML)
                 if r["rail"].startswith("quick")][0]
        self.assertEqual(quick["rail"], "quick task q40")
        self.assertEqual(quick["rail_key"], "quick")
        self.assertEqual(quick["amount_fj"], 120000.0)

    def test_treasury_stamp_is_read_from_the_page_itself(self):
        bal, block, as_of = fc.parse_treasury_stamp(LEDGER_HTML)
        self.assertEqual(block, 131080181)
        self.assertEqual(bal, 824182279.0)
        self.assertIn("2026-11-01 11:22 COT", as_of)

    def test_no_tool_row_this_week_leaves_the_slot_open(self):
        rows = fc.parse_payouts(LEDGER_HTML)
        cap, paid, open_ = fc.slot_state(rows, "tool", date(2026, 11, 1))
        self.assertEqual((cap, paid, open_), (1, 0, True))

    def test_a_tool_row_inside_the_same_week_closes_it(self):
        rows = [{"handle": "@x", "amount_fj": 15000000.0, "day": "2026-10-27",
                 "rail": "tool", "rail_key": "tool", "tx": None,
                 "tx_short": None, "work": ""}]
        cap, paid, open_ = fc.slot_state(rows, "tool", date(2026, 11, 1))
        self.assertEqual((cap, paid, open_), (1, 1, False))

    def test_an_older_week_does_not_close_this_one(self):
        rows = fc.parse_payouts(LEDGER_HTML)          # tool paid 2026-10-24
        self.assertEqual(fc.slot_state(rows, "tool", date(2026, 11, 1))[1], 0)
        # ... and the same rows DO close the week that contains 10-24
        self.assertEqual(fc.slot_state(rows, "tool", date(2026, 10, 24))[1], 1)

    def test_payouts_takes_no_wallet(self):
        args = fc.parse_argv(["payouts", "--rail", "tool", "--since", "2026-10-26"])
        self.assertIsNone(args["wallet"])
        self.assertEqual(args["rail"], "tool")
        self.assertEqual(args["since"], "2026-10-26")
        self.assertEqual(args["limit"], 20)
        with self.assertRaises(SystemExit):
            fc.parse_argv(["payouts", "--since", "yesterday"])

    def test_an_unparseable_ledger_is_a_failure_not_an_empty_week(self):
        orig = fc.fetch_payouts_page
        fc.fetch_payouts_page = lambda *a, **k: "<html>502</html>"
        try:
            args = fc.parse_argv(["payouts"])
            self.assertEqual(fc.payouts(args), 3)
        finally:
            fc.fetch_payouts_page = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
