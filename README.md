# Intents Execution Desk

An execution agent for DAO treasuries on [NEAR Intents](https://docs.near-intents.org/).
It sells a large treasury position (for example RHEA -> USDC) in small slices over a few hours
instead of one swap, waits for liquidity to come back between slices, never signs a quote that
is far off the market, and reports the cost of execution (TCA) against a single swap.

Built at NEAR HACKATHON (Nov 1-14, 2026). Track: Unstoppable DeFi.

**Live dashboard:** https://intents-execution-desk.onrender.com
(free hosting; after a long idle period the first load can take up to a minute)

## The problem, in numbers

A project or DAO that needs dollars for salaries or runway sells its own token. One big swap
pays a large price impact, or does not go through at all. 1Click dry quotes, selling to USDC,
loss vs. the reference price including the 1Click fee (Oct 23-24, 2026):

| Size | wNEAR | RHEA | AURORA |
| --- | --- | --- | --- |
| $500 | ~0 bps | 140 bps | 152 bps |
| $5,000 | ~6 bps | 208 bps | 462 bps |
| $25,000 | ~11 bps | 503 bps | 1,550 bps |
| $100,000+ | ~22 bps | no liquidity | no liquidity |

On Nov 3, 2026 a $25,000 RHEA sell had **no quote at all** as a single swap.

## Results on live quotes (shadow mode, RHEA -> USDC, $25k, 2 hours, Nov 3, 2026)

| | Run 1: $5k slices | Run 2: ~$1.2k slices |
| --- | --- | --- |
| Slices | 4 | 21 |
| Pace | 80% sold in 46 min (ran ahead) | on schedule, done in 1 h 44 min |
| Execution cost vs benchmark | 128 bps | **33 bps** |
| One swap at the start | impossible (no liquidity) | impossible (no liquidity) |

Run 1 exposed two policy bugs (taking good quotes while ahead of schedule; slices too large).
Both are fixed and covered by tests; run 2 is the fixed agent.

## Try it

**On the live dashboard (no setup, no funds):**

1. Open the dashboard, expand **Start a shadow order** at the bottom.
2. Pick RHEA, $25,000, 2 hours, keep the limits, press **Start shadow order**.
   You see the impact curve measured right now, with the chosen slice size marked.
3. Wait a few minutes and the page refreshes itself: slices appear on the price chart, **Schedule
   vs sold** tracks the plan, and the **Decision log** explains every tick (`EXECUTE good quote`,
   `WAIT liquidity not recovered`, `WAIT quote worse than hard limit`...).
4. The TCA panel splits the cost into timing (the market moved) and execution (spread, impact,
   fees) and compares it with one swap at the start.

**Locally:**

```
pip install -r requirements.txt
cp .env.example .env               # put a 1Click API key into ONECLICK_API_KEY
python -m desk.cli curve RHEA      # impact curve from dry quotes right now
python -m desk.cli new-order RHEA --usd 25000 --to treasury.sputnik-dao.near --hours 2
python -m desk.cli run             # the agent; Ctrl+C and restart any time
python -m desk.cli status          # progress and TCA in the terminal
streamlit run ui/app.py            # the dashboard
pytest                             # 44 tests, no network needed
```

## How it works

```
DAO treasury (Sputnik DAO) --budget + limits--> agent (Render, 24/7)
                                                  |  benchmark: micro-mid from small dry quotes
                                                  |  policy: trade now or wait
                                                  |  guard: compare live quote with dry quote
                                                  v
                                           1Click API --solvers compete--> intents.near (settles)
                                                  |
                                     USDC goes straight to the treasury account
```

1. **Order.** Token, size, horizon, soft and hard limits (bps vs the benchmark), and the
   treasury account that receives the output.
2. **Benchmark (micro-mid).** The reference price from `GET /v0/tokens` jumps by 40-100 bps
   while real depth stays flat, so the agent builds its own: dry quotes at $300, $1k and $3k,
   the 1Click fee added back, a straight line extended to size 0. Refreshed every 60 s.
3. **Impact curve and slice size.** At creation the agent quotes $500...$25k, picks the
   smallest slice that fits the horizon (about one slice per 5 minutes), capped by the soft limit.
4. **Policy, every 20 s** (`desk/engine/policy.py`): linear schedule ending at 90% of the
   horizon. Trade when not ahead of schedule and the quote is within the soft limit; trade
   anything within the hard limit when a full slice behind; never beyond the hard limit; at
   least 60 s between slices; after a slice, wait until the quote for the next slice comes back
   to its pre-trade level (liquidity recovered).
5. **Guard** (`desk/engine/guard.py`): on Oct 24, 2026 a live 1Click quote for 1,000 RHEA came
   back at 27.91 USDC while dry quotes said ~63.6, 56% worse. `slippageTolerance` does not catch
   that, it is measured from the quote itself. The agent compares every live quote with a fresh
   dry quote and the benchmark and refuses to sign if it is off.
6. **Execution (live mode):** live quote -> guard -> `generate-intent` -> NEP-413 signature by
   the agent key -> `submit-intent` -> `GET /v0/status`. The agent trades from its NEAR Intents
   balance (`depositType: INTENTS`), so there is no on-chain deposit per slice. Its account is an
   implicit account (hex of the ed25519 public key): no registration needed.
7. **Restarts:** the slice status is written before each external call; after a restart signed or
   submitted slices are reconciled through `/v0/status`, never sent twice (tested).
8. **TCA** (`desk/tca/metrics.py`): implementation shortfall = timing + execution, exactly,
   all in bps of the arrival price; savings vs one swap at the start.

## NEAR features used

- **NEAR Intents 1Click API:** dry and live quotes, `generate-intent` / `submit-intent`, status.
- **NEP-413 signing** in Python (`desk/near/signer.py`), checked on Oct 24, 2026 against
  `simulate_intents` on `intents.near`: a valid signature reaches the balance check, a corrupted
  one fails with `invalid signature`.
- **Implicit accounts** for the agent.
- **Sputnik DAO** treasury as the recipient of every slice.

## Limitations (read before trusting the numbers)

- **The demo runs in shadow mode.** Decisions use real 1Click dry quotes; fills are recorded at the
  dry quote and nothing is signed. The live path is implemented and tested against a simulated
  pool, but has not run with real funds.
- **Shadow mode cannot see the agent's own footprint.** Each shadow slice also stores a worst case
  (output if liquidity never recovered between slices); real execution lands between the two.
  When the cumulative size has no liquidity as one swap, the worst case is not available.
- **1Click is custodial for a moment:** funds sit with the 1Click deposit account during a swap.
- **The agent key lives on the server** (env variable). Keep only the order budget on it; the
  output goes straight to the treasury.
- **Timing cost includes our own footprint on thin markets**; splitting temporary and permanent
  impact is future work.

## Layout

```
desk/
  config.py            settings from environment, asset ids
  oneclick/client.py   1Click API client
  near/signer.py       NEP-413 signing, implicit account
  prices/benchmark.py  micro-mid benchmark, impact curve
  engine/policy.py     adaptive TWAP decision per tick
  engine/guard.py      live-quote check before signing
  engine/desk.py       orders, ticks, shadow and live execution, restart reconciliation
  tca/metrics.py       implementation shortfall, timing, execution, savings
  db/models.py         orders, slices, quotes, decision log (SQLite locally, Postgres on Render)
  worker.py, cli.py    always-on loop, command line
ui/app.py              Streamlit dashboard
start.sh, render.yaml  one free Render web service: worker + dashboard
.github/workflows/     keepalive ping so the free service does not sleep
tests/                 unit and engine tests with a fake constant-product pool
```
