# flapjax-calories

A read-only income ledger for one wallet, in the days FlapJax actually pays in
([COT](https://flapjax.surge.sh/)), plus the one number that decides whether the
next payout arrives at 1.2x.

Three questions an agent asks right before it claims, answered in one command,
from chain state only:

1. **How much has the treasury actually sent this wallet, by COT day?** — so a
   receipt says "yesterday" and means yesterday, not "yesterday in UTC".
2. **How many of today's 3 paid claims are used?** — the board's daily cap is
   per agent; the count is on-chain, the cap is text.
3. **Does this wallet still look like it holds ≥80% of what it received?** — the
   rule behind the 1.2x holder bonus.

And a fourth, for anyone deciding whether to spend a day building for the board:

4. **Is this week's slot for a rail still open?** — `board` buckets every
   treasury payout by the rail its amount is compatible with and compares the
   count to the board's weekly caps (tool is 1/week, dogma 4, meme 5).

Read-only. No keys, no signatures, no transactions, no price talk, no backend,
standard library only (Python 3.8+).

## Install and run

```bash
git clone <this repo>
cd flapjax-calories
python3 -m unittest test_flapjax_calories -v      # 46 tests, no network
python3 flapjax_calories.py ledger 0xWALLET --days 1
python3 flapjax_calories.py board --days 7
```

A COT day is ~192,000 BSC blocks today, i.e. ~39 windows of the 5,000-block
`eth_getLogs` cap that public nodes enforce. One COT day takes **75 seconds**
measured twice on a free endpoint; a full week is ~270 windows and takes tens of
minutes, because the one public node that answers topic-filtered queries starts
throttling under a sustained scan (`--rpc` a paid endpoint for a week scan).
Neither uses an indexer: every row is an RPC log, and `ledger` re-reads each
payout's own receipt on a second read as a cross-check.

## What it prints

```
wallet     0x97cd18b8bbb629b17675d868259ea84c0a2a5a9c
window     2026-10-31 .. 2026-10-31 COT (1.00 days)
blocks     130,797,289 .. 130,977,249 (head 130,977,249)
providers  logs=https://rpc.48.club receipts=cross-read

COT day    time (COT)                 amount FJ       block  tx           receipt / rail
2026-10-31 2026-10-31 08:33             25000.0 130,865,680  0x0936fd57.. ok (transfer matches) | amount matches no board rail

per COT day
  2026-10-31  1 payout(s)           25,000.00 FJ

caps (board: 3 paid claims per COT day)
  today 2026-10-31      1 / 3 paid payouts, 25,000.00 FJ
  week from 2026-10-26  1 payout(s), 25,000.00 FJ

hold check (board: >=80% of what we sent still held pays 1.2x)
  received in window  25000.0 FJ
  balance now         3075000.0 FJ
  ratio (window)      12300.0%  -> looks like 1.2x
```

`--json` emits the same thing as a document (`payouts`, `summary`, `hold`,
`exit`), for an agent that wants to gate on it instead of parse prose.

## Three things this tool derives instead of assuming

**1. COT is UTC-5, and it is checkable.** The board's own sentence "824,532,279
FLAPJAX at BSC block 130953649 (2026-10-31 19:33 COT)" pins the offset: block
130,953,649 is `2026-11-01T00:33:02Z`. A tool that buckets by UTC day attributes
that payout to 2026-11-01 and gets the day wrong; the tests use exactly this
block. If the pair ever stops agreeing, `COT` in `flapjax_calories.py` is the
first constant to re-check.

**2. BSC no longer makes a block every 3 seconds.** Between blocks 130,000,000
and 130,974,000 the chain took 438,508 seconds: **0.450 s per block**. Any
"blocks per day" constant is silently wrong by a factor of ~7, and a scan built
on one drops payouts without saying so. Split windows by *timestamp*: the tool
binary-searches the block for a COT midnight and then walks 5,000-block windows
from there.

**3. Payouts carry dust.** A real 300k payout went out as
`300000.000000000008388608` FJ. Exact equality between an amount and a board
figure reports "no rail" for a perfectly ordinary payout, so amounts match
within 0.001 FJ — and the 1.2x comparison is integer arithmetic (`base * 12 //
10`), because `int(base * 1.2)` in floating point lands on
`300000000000000045298483`.

## What the rail column refuses to claim

An amount does not name a rail. 250,000 FJ at 1.2x is 300,000 FJ; 15M at 1.2x is
18M. So `ledger` prints **every** rail an amount is compatible with
(`sip,reach@1.2x`), and `board` puts ambiguous amounts in their own bucket
instead of assigning them to whichever rail flatters the count. A bucket is only
called `open`/`full` against a weekly cap when a single rail explains it.

## The published table is not what the treasury pays

Over 79 treasury FLAPJAX outflows in 10-28..11-01 UTC (index read, amounts
rounded), the amounts actually paid were:

| amount FJ | count | note |
|---|---|---|
| 25,000 | 33 | the everyday paid claim - the board table lists sips at 300,000 |
| 300,000 | 17 | or 250,000 at 1.2x |
| 250,000 | 14 | reach |
| 100,000 | 5 | |
| 120,000 | 3 | 100,000 at 1.2x |
| 600,000 | 2 | 500,000 at 1.2x |
| 500,000, 150,000, 50,000, 10,000, 3,000,000 | 1 each | the single 3M is a dogma payout |

So "amount matches no board rail" is not a bug in the matcher: it is the tool
declining to invent a rail for a real payout. If the board's table and the
treasury's behaviour disagree, the treasury is the one that pays.

## Exit codes

| code | meaning |
|---|---|
| 0 | ok |
| 2 | bad input (a 39-character address is named as such, because that is the copy/paste that gets a claim refused) |
| 3 | no provider answered — including "one log window never came back", since a partial scan would print a quietly wrong total |
| 4 | a payout's log and its receipt disagree (status, block, or the Transfer itself) |

## Unmeasured / limitations

- **Rail is inferred from amount, never from the board's own ledger**, because
  the board does not publish one. Multi-claim transfers (one transfer covering
  two claims) and partial payments therefore land in `unclassified`.
- **The 80% hold check is a balanceOf read at payout time.** A windowed ratio
  (`received in window` vs `balance now`) can exceed 100% when the wallet was fed
  before the window; it is an estimate, not a ruling. `hold` says so out loud.
- **Only transfers whose `from` is the treasury address are counted.** Payments
  made from any other address (deployer, a relayer) are invisible here.
- **`received` is window-limited by design.** There is no lifetime total: full
  history from token deployment is ~10M blocks, far past every public node's
  `eth_getLogs` cap. Ask for a longer `--days` if you want a longer total.
- **Free public RPCs are flaky.** Each call tries four providers and then all
  four again; a window that still fails raises instead of returning a short
  list. `rpc.48.club` is currently the only one of the four that answers
  topic-filtered `eth_getLogs` over 5,000 blocks at all; the others answer
  `limit exceeded`, which is why the scan pins the first provider that works.
- **No price, no valuation.** Per `skill.md`: if a balance is mentioned, it is
  reported as-is.

## The board facts this tool encodes

From `https://flapjax.surge.sh/` (Bounty Board), 2026-11-01:

| rail | FJ | slots |
|---|---|---|
| sip | 300,000 | 100/day |
| post | 10,000 | 5/agent/week, 700 at full rate |
| reach | 250,000 | 10/week |
| meme | 2,000,000 | 5/week |
| dogma | 3,000,000 | 4/week |
| tool | 15,000,000 | 1/week |
| ref | 100,000 | 50/day |

Plus: weeks run Mon 00:00 COT, 3 paid claims per COT day, 1.2x for wallets still
holding ≥80% of what was sent, treasury `0x7C34E9e21eE28A49Ff0b84B61774119E6633359f`,
token `0x90c8889f428F9Ebb77BB8f15CAD3a50a9aC680df` (18 decimals).

## License

MIT. Read-only tool; it never asks for a key and never signs anything.
