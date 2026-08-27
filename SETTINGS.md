# Settings reference

Every setting, what it does, and which way to move it.

Seeded from `.env` on first run, then owned by the database. Editing `.env`
afterwards has no effect — change them from Telegram.

Numbers cited as "measured" were recorded against live mainnet data by the
scripts in `spike/`; the recorded ticks they came from are replayed in
`tests/test_replay.py`.

---

## Thresholds

A dip fires when price drops this far below the **rolling high within the
window** — not below the price at the window's start. A coin that pumps 40% then
dumps 25% is exactly the alert you want; comparing against the window's opening
price would miss it entirely.

### `threshold_migrated_10s` / `_1m` / `_3m` / `_5m` — 8 / 15 / 22 / 30 %

One number per window. Shown in `/settings` as **drop**; the key keeps the
`migrated` word so existing databases keep their tuned values.

Your original 5/10/15/20 produced **53 alerts in 30 minutes from 11 tokens**
(~106/hour). These seeds are raised from that but are still a guess — the honest
number comes from tuning against recorded ticks.

**Lower** = more alerts, more noise. **Raise** = only real crashes.

### Coins that have not bonded

Not alerted on at all, and not even subscribed. Measured three-minute swings on
pre-bond coins were **49%, 71% and 93%** — no threshold is meaningful against
that, so tracking begins the moment a coin bonds. `/list` shows them as ⏳.

---

## Windows and quoting

### `enabled_windows` — `10s,1m,3m,5m`

Which lookback windows are evaluated at all. Removing one silences that class of
alert globally.

🐢 polled tokens ignore `10s` and `1m` regardless — DexScreener's data is
CDN-cached for 30s, so those windows are not physically supported. The bot
refuses to pretend otherwise.

### `quote_mode` — `SOL`

Whether a dip is measured in SOL or USD.

Defaults to **SOL on purpose**: if SOL itself drops 5%, every token shows a
phantom USD dip and the channel lights up for nothing. SOL-denominated isolates
the token's own move.

Switch to `USD` only if you care about your position's dollar value rather than
the token's behaviour.

---

## Alert gating

### `cooldown_per_token_seconds` — `600`

After a token alerts, silence it for this long **across all windows**.

This is the single most important noise control. Per-window cooldowns don't work:
one PKG dump fired `10s`, `3m` and `5m` at an identical −22.5% from the same
tick — three messages, one event. The fastest window to trip wins and suppresses
the rest.

### `rearm_recovery_pct` — `50`

After alerting, don't alert again until price recovers this share of the drop.

Without it, a coin grinding steadily downward re-fires every time the cooldown
expires. At 50, a token that fell 20% must bounce back 10% before it can alert
again.

### `warmup_seconds` — `60`

Ignore a token for this long after startup, or after a feed gap.

Price history lives in memory, so a fresh buffer has no past to compare against —
the first tick would look like a catastrophic dip. This suppresses that.

### `confirm_ticks` — `1`

Require the drop to still hold after this many additional ticks.

Costs a couple of seconds of latency, kills single bad prints. Set `0` for
fastest possible alerts and more false positives.

### `min_ticks_10s` / `_1m` / `_3m` / `_5m` — 3 / 8 / 15 / 20

Minimum price samples that must exist in a window before it's judged.

Scales with window length deliberately: during testing a 3m alert fired on
exactly **4 ticks**, which is not a trend, it's three data points and a rumour.

**Raise** = fewer alerts on thin data. **Lower** = catches dips on quiet tokens,
with more noise.

---

## Token filters

Applied before a token can alert at all.

### `filters_enabled` — `on`

Master switch. Off means every watched coin can alert, thin pools included.
Toggle from `/settings` → 🚫 Filters, or `/filters off`.

### `filter_liquidity_enabled` / `filter_volume_enabled` / `filter_age_enabled` — `on`

Each filter has its own switch as well as its value, so one can be turned off
without losing the number you had tuned.

### `min_liquidity_usd` — `$10,000`

Thin pools print fake wicks constantly — a $500 pool "drops 40%" on a $50 trade.

Note: DexScreener sometimes reports **no liquidity figure at all** (MEMEDEX on
`meteoradbc` is a live example). Unknown is not zero, and the bot handles that as
an explicit case rather than silently treating it as $0.

### `unknown_liquidity` — `proxy`

What to do when DexScreener reports no liquidity figure at all — real, and not
rare: MEMEDEX on `meteoradbc` showed $141k of volume with a null liquidity.

`proxy` judges the coin on volume instead, `skip` blocks it, `allow` passes it.
Treating null as $0 would silently mute legitimate coins.

### `min_volume_24h_usd` — `$50,000`

Same idea over a longer horizon. Filters tokens that technically have liquidity
but nobody trading them.

### `min_token_age_minutes` — `0` (disabled)

Skip tokens younger than this.

Worth raising if the channel is too busy. Note that pre-bond coins are already
excluded entirely, so this only affects freshly bonded ones.

### `outlier_median_multiple` — `3`

Discard a tick deviating more than this multiple from the recent median, unless
the next tick confirms it. Catches sandwich and MEV prints.

Related but separate: on pool tokens each trade updates two vault accounts as two
messages, and pricing a half-updated pool produced a phantom **30.45%** swing
where the true one was 0.83%. That's fixed structurally by slot grouping, not by
this filter.

---

## Quick tuning guide

| Symptom | Turn this |
|---|---|
| Too many alerts | raise thresholds first, then `cooldown_per_token_seconds` |
| Same dump alerts repeatedly | raise `rearm_recovery_pct` |
| One event, several messages | raise `cooldown_per_token_seconds` |
| Alerts on junk tokens | raise `min_liquidity_usd` / `min_volume_24h_usd` |
| Alerts on brand-new launches | raise `min_token_age_minutes` |
| Alerts feel too slow | lower `confirm_ticks` to 0 |
| Alerts on 2-3 data points | raise `min_ticks_*` |
| Everything dips when SOL dips | check `quote_mode` is `SOL` |
