# Intents Execution Desk

Execution agent for DAO treasuries on [NEAR Intents](https://docs.near-intents.org/).
It sells a large treasury position (for example RHEA -> USDC) in small slices over hours
instead of one swap, waits for liquidity to recover between slices, and produces a
transaction cost analysis (TCA) report: implementation shortfall vs. a single swap.

Built at NEAR HACKATHON (Nov 1-14, 2026), track: Unstoppable DeFi.

> Work in progress. This README is expanded as the project is built.

## Why

Measured on Oct 23-24, 2026 with 1Click dry quotes (sell to USDC, loss vs. reference price,
including the 1Click fee):

| Size | wNEAR | RHEA | AURORA |
| --- | --- | --- | --- |
| $500 | ~0 bps | 140 bps | 152 bps |
| $5,000 | ~6 bps | 208 bps | 462 bps |
| $25,000 | ~11 bps | 503 bps | 1,550 bps |
| $100,000+ | ~22 bps | no liquidity | no liquidity |

For mid-cap ecosystem tokens a single large swap is expensive or impossible.

## Layout

```
desk/
  config.py            settings from environment
  oneclick/client.py   1Click API client (tokens, quote, generate/submit intent, status)
  near/signer.py       NEP-413 signing with the agent's ed25519 key
  prices/benchmark.py  benchmark mid from small dry quotes, impact curve
  db/models.py         parent orders, slices, quote snapshots
  engine/policy.py     adaptive TWAP: trade now or wait (schedule, limits, liquidity recovery)
  engine/guard.py      refuses live quotes that are far worse than dry quotes / benchmark
  engine/desk.py       order creation, ticks, shadow and live execution, restart reconciliation
  tca/metrics.py       implementation shortfall = timing + execution, savings vs one swap
  worker.py            always-on loop
  cli.py               command line
tests/                 unit tests (no network)
```

## Try it

```
pip install -r requirements.txt
cp .env.example .env          # put your 1Click API key into ONECLICK_API_KEY
python -m desk.cli curve RHEA # benchmark + impact curve from dry quotes, no funds needed

# shadow order: real dry quotes, nothing is signed or sent, no funds needed
python -m desk.cli new-order RHEA --usd 25000 --to treasury.sputnik-dao.near --hours 4
python -m desk.cli run        # worker, Ctrl+C to stop; restart any time
python -m desk.cli status     # progress and TCA report (in another terminal)
```

Shadow mode cannot see the agent's own footprint, so each shadow slice also records a
worst case: the output if liquidity never recovered between slices. The real result
lands between the two numbers in the report.

## Run tests

```
pytest
```
