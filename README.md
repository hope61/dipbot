# dipbot

Self-hosted Telegram bot that watches Solana tokens and posts an alert when one
drops sharply. You run the instance, you control the watchlist, alerts go to a
channel you own.

```
Cate -23% in 1m

MC: 501k -> 384k
ATH: 1.2M
Age: 3h 18m
Vol 5m: 41k
Vol 24h: 2.3M

Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump

[ Chart ] [ pump.fun ] [ Trojan ] [ 🔇 Mute 1h ] [ 🗑 Remove ]
```

- Per-trade pricing from chain state over `accountSubscribe`, not just polling.
- Four dip windows: 10s, 1m, 3m, 5m, each with its own threshold.
- Every price cross-checked against a second source before anything is sent.
- Managed entirely from Telegram; no redeploy to change a threshold.
- Runs in Docker on a named volume. 528 offline tests.

## Requirements

Python 3.12 or newer, or Docker. A Telegram bot token and a channel. No paid API key is
required to start, though one is recommended for the RPC endpoint (below).

## Install

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
.venv/bin/python -m dipbot.main
```

With Docker, which is how it runs in production:

```bash
docker compose up -d --build
docker compose logs -f
```

SQLite lives on a named volume, so image rebuilds and `docker compose down`
keep the watchlist, tuned settings and alert history. `.env` stays on the host
and is never baked into the image. The container runs as a non-root user, logs
are capped at 3×10MB, and SIGTERM shuts it down inside the 30s grace period.

Back up or migrate the whole bot:

```bash
docker run --rm -v dipbot_dipbot-data:/data -v "$PWD:/out" alpine tar czf /out/dipbot-backup.tar.gz -C /data .
```

## Configuration

Three values are required, all from Telegram:

| Variable | Where to get it |
|---|---|
| `TELEGRAM_BOT_TOKEN` | @BotFather → `/newbot` |
| `TELEGRAM_CHANNEL_ID` | add the bot to your channel as an admin with Post Messages, then forward any channel message to @userinfobot |
| `OWNER_USER_ID` | message @userinfobot |

Everything else has a working default. Worth reviewing:

| Variable | Default | Notes |
|---|---|---|
| `SOLANA_RPC_HTTP` | public endpoint | Returns 429 under bursts. Use a provider key; a Helius free key costs nothing and needs no card. |
| `SOLANA_RPC_WS` | public endpoint | Fine as-is. Held 22 subscriptions for 30 minutes with zero disconnects in testing. |
| `QUOTE_MODE` | `SOL` | Price dips in SOL, not USD. If SOL itself drops 5%, every token shows a phantom USD dip at once. |
| `THRESHOLD_MIGRATED_10S/1M/3M/5M` | 8 / 15 / 22 / 30 | Percent drop from the rolling high in each window. |
| `MIN_LIQUIDITY_USD` | 10000 | Thin pools print fake wicks constantly. |

**These are seed values only.** They are read from `.env` on the first run,
written to SQLite, and owned by the database from then on. Editing `.env`
afterwards has no effect — change them with `/set` or the `/settings` editor.
When debugging why something did not alert, read the database, not `.env`.

Full reference: [SETTINGS.md](SETTINGS.md).

## Commands

Anyone who is not the owner or an admin gets one refusal and is otherwise
ignored.

| Command | Effect |
|---|---|
| `/add <mint \| pair \| url>` | track a token — or just paste an address |
| `/remove <symbol \| mint>` | stop tracking one |
| `/clear` | stop tracking everything |
| `/list` | watchlist with tier badges |
| `/status` | uptime, feed health, tick counters, errors |
| `/settings` | current values, with an inline editor |
| `/set <setting> <value>` | change one setting |
| `/token <symbol>` | per-token threshold overrides |
| `/filters <on\|off>` | apply or ignore the token filters |
| `/mute <30m\|2h>`, `/unmute` | silence alerts temporarily |
| `/summary` | post the daily summary now |
| `/test` | post a sample alert to the channel |
| `/guide`, `/help` | in-bot documentation |
| `/admins`, `/addadmin <id>`, `/removeadmin <id>` | manage access (owner only) |

Admins get full control of the watchlist and settings but cannot manage other
admins. The owner comes from `OWNER_USER_ID`, is never stored in the database
and cannot be removed, so an admin cannot lock you out. Permissions are read
fresh on every update, so revoking an admin takes effect immediately.

## How it works

Every feed produces a `PriceTick`. The detector consumes ticks and nothing else
— no clock, no network — so a recorded market can be replayed through it at full
speed to tune thresholds against real data.

Each token sits in one of two tiers, and the tier decides which windows may fire:

| Tier | Priced from | Windows |
|---|---|---|
| ⚡ realtime | chain state, per trade, over `accountSubscribe` | 10s, 1m, 3m, 5m |
| 🐢 polled | DexScreener, CDN-cached 30s | 3m, 5m only |

Polled data cannot support a 10-second window, so the detector refuses those
windows rather than emitting alerts the data does not support. Tokens are
promoted to realtime automatically as they become priceable. Realtime currently
covers pump.fun bonding curves and PumpSwap pools; concentrated liquidity venues
(Raydium CLMM, Orca Whirlpool, Meteora DLMM) price from `sqrtPriceX64` or an
active bin rather than vault ratios, so they stay polled.

Detector rules, each added after a specific failure in testing:

| Rule | Reason |
|---|---|
| Dip measured from the rolling high **inside the window**, not the opening price | A coin that pumped then gave it back should read as the round trip it was. |
| Global per-token cooldown | One move fired 10s, 3m and 5m at an identical −22.5%. |
| Unconfirmed outliers discarded | A single bad print manufactures a dip out of nothing. |
| Pre-bond coins skipped entirely | They swung 49%, 71% and 93% inside three minutes; no threshold is meaningful. |
| Liquidity and volume floors | Thin pools wick constantly. |

Two guards exist because wrong figures were published once. Chain price is
compared against DexScreener on every poll and a token whose *median* ratio
disagrees is suppressed rather than guessed at. Market caps are anchored to
DexScreener's own number moved by our price change since, never derived from a
ratio.

A token's identity for polling is its **pair address, not its mint**. Asking
DexScreener about a migrated pump.fun coin by mint can return the abandoned
bonding-curve pair: frozen price, frozen cap, no dip ever detectable.
Resolution therefore ranks a real pool above any curve pair, and the poller
re-resolves anything still on a curve so it follows the migration.

## Tuning

Thresholds are tuned by replay against recorded ticks, not by guessing.
The recordings themselves are not in the repo — they are several MB of live
market capture and only mean anything next to the run that produced them — so
record your own sample first:

```bash
python3 spike/record_bands.py 30
```

That writes ticks for three liquidity bands (fresh bonding curves, thin
migrated pools, established pools) to `spike/data/`. Then:

```bash
python3 spike/replay.py --sweep
```

This prints alert counts across a grid of thresholds against that sample, so
you can see what a change would have done to a real market before shipping it.
`--band thin` restricts it to one band.

## Troubleshooting

The bot logs three lines a minute:

```
feed connected=True accounts=32 tokens=16 ticks=466 notif=958 reconnects=0
detector tokens=17 buffered=619 suppressed={'warmup':192,'filtered':45,'cooldown':33}
ticks/60s CATE=31 Pistacio=26 CRYPTO=18 Beni=66 BTCBULL=3 ...
```

`suppressed` names the rule that ate the alert. A coin sitting at exactly 3
ticks/min is on polled data only, whatever tier it claims: it lost its realtime
subscription and is now below every window's minimum tick count, so it will not
alert while still appearing watched.

## Development

```bash
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest
```

528 tests, fully offline, a few seconds to run. No network, no Telegram, no real
`.env`; HTTP layers are stubbed per module and anything needing a live API lives
in `spike/` rather than the suite.

`tests/test_consistency.py` fails when the docs or the in-bot UI drift from the
code, so adding or renaming a setting means updating `config.py`,
`bot/settings_ui.py`, `bot/guide.py` and `SETTINGS.md` together.
`tests/test_replay.py` pushes 2,378 recorded ticks through the detector and
asserts *ranges* rather than exact alert counts, which catches "fires wildly
more" and "stopped firing" without breaking on deliberate tuning.

```
dipbot/
  config.py               env loading and validation
  models.py               PriceTick, TokenMeta, Tier — the feed seam
  detector.py             dip rules; every guard documents why it exists
  sanity.py               cross-source price check, market cap anchoring
  db.py                   SQLite; ticks are deliberately not persisted
  filters.py              token eligibility, incl. unknown-liquidity policy
  solana.py               base58 + program-derived addresses
  poller.py               DexScreener metadata refresh loop
  alerts.py               formatting + rate-limited channel sender
  ops.py                  credit budget guard, health monitor, daily summary
  feeds/dexscreener.py    resolution, batching, cached-empty handling
  feeds/solana_rpc.py     JSON-RPC over HTTP (low volume)
  feeds/pool_resolver.py  curve/pool detection, vault discovery
  feeds/rpc_feed.py       accountSubscribe stream, slot grouping
  feeds/supervisor.py     keeps subscriptions in step with the watchlist
  bot/handlers.py         commands
  bot/settings_ui.py      inline settings editor
  bot/middleware.py       owner/admin gate
```

## License

MIT — see [LICENSE](LICENSE).
