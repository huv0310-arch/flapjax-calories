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


if __name__ == "__main__":
    unittest.main(verbosity=2)
