"""Settings read from the environment (and a local .env file, if present)."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()

ONECLICK_BASE_URL = "https://1click.chaindefuser.com/v0"
# Cloudflare in front of 1Click rejects the default Python User-Agent (error 1010).
USER_AGENT = "intents-execution-desk/0.1"

# Asset ids on NEAR (from GET /v0/tokens)
USDC_NEAR = "nep141:17208628f84f5d6ad33f0da3bbbeb27ffcb398eac501a31bd6ad2011e36133a1"
RHEA = "nep141:token.rhealab.near"
AURORA = "nep141:aaaaaa20d9e0e2461697782ef11675f668207961.factory.bridge.near"
WNEAR = "nep141:wrap.near"


@dataclass(frozen=True)
class Settings:
    oneclick_api_key: str
    database_url: str
    agent_seed_hex: str
    mode: str  # "shadow" | "live"
    daos: tuple[str, ...] = ()       # Sputnik DAO accounts whose approved proposals become orders
    dao_network: str = "testnet"
    series: tuple[tuple[str, float], ...] = ()   # (token, usd) rotated through by the evidence series
    series_hours: tuple[float, ...] = (0.5, 1.0, 2.0, 4.0)

    @property
    def is_live(self) -> bool:
        return self.mode == "live"


def load_settings() -> Settings:
    mode = os.environ.get("DESK_MODE", "shadow").strip().lower()
    if mode not in ("shadow", "live"):
        raise ValueError(f"DESK_MODE must be 'shadow' or 'live', got {mode!r}")
    return Settings(
        oneclick_api_key=os.environ.get("ONECLICK_API_KEY", "").strip(),
        database_url=os.environ.get("DATABASE_URL", "sqlite:///desk.db").strip(),
        agent_seed_hex=os.environ.get("AGENT_SEED_HEX", "").strip(),
        mode=mode,
        daos=tuple(d.strip() for d in os.environ.get("DESK_DAOS", "").split(",") if d.strip()),
        dao_network=os.environ.get("DESK_DAO_NETWORK", "testnet").strip() or "testnet",
        series=parse_series(os.environ.get("DESK_SERIES", "")),
        series_hours=tuple(float(h) for h in os.environ.get("DESK_SERIES_HOURS", "0.5,1,2,4").split(",") if h.strip()),
    )


def parse_series(spec: str) -> tuple[tuple[str, float], ...]:
    """"RHEA:25000,AURORA:10000" -> (("RHEA", 25000.0), ("AURORA", 10000.0))"""
    out = []
    for part in spec.split(","):
        if part.strip():
            sym, usd = part.split(":")
            out.append((sym.strip(), float(usd)))
    return tuple(out)
