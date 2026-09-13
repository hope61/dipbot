"""Budget guard, health monitor, daily summary.

All three exist to make quiet failures loud, so the tests care mostly about
*when* they speak and when they stay silent.
"""
from __future__ import annotations

import time
from datetime import datetime

import pytest

from dipbot.ops import (
    CREDITS_PER_MB,
    BudgetGuard,
    DailySummary,
    HealthMonitor,
    SummaryData,
    render_summary,
    seconds_until,
)



class FakeFeed:
    def __init__(self, bytes_received=0, using_fallback=False, connected=True):
        self.bytes_received = bytes_received
        self.using_fallback = using_fallback
        self.connected = connected
        self.fallback_url = "wss://fallback"

    def stats(self):
        return {
            "uptime_s": 3600.0,
            "megabytes": self.bytes_received / 1e6,
            "reconnects": 2,
        }


class Recorder:
    def __init__(self):
        self.messages: list[str] = []

    async def __call__(self, text: str) -> None:
        self.messages.append(text)


# --- budget guard -----------------------------------------------------------


async def test_public_endpoint_traffic_is_free(db):
    feed = FakeFeed(bytes_received=50_000_000, using_fallback=False)
    guard = BudgetGuard(db, feed, Recorder(), budget=1000)
    assert await guard.sample() == 0.0


async def test_fallback_traffic_is_metered(db):
    feed = FakeFeed(bytes_received=1_000_000, using_fallback=True)
    guard = BudgetGuard(db, feed, Recorder(), budget=1e9)
    assert await guard.sample() == pytest.approx(CREDITS_PER_MB)


async def test_usage_accumulates_across_samples(db):
    feed = FakeFeed(bytes_received=1_000_000, using_fallback=True)
    guard = BudgetGuard(db, feed, Recorder(), budget=1e9)
    await guard.sample()
    feed.bytes_received = 3_000_000
    assert await guard.sample() == pytest.approx(3 * CREDITS_PER_MB)


async def test_usage_persists_in_the_database(db):
    feed = FakeFeed(bytes_received=1_000_000, using_fallback=True)
    await BudgetGuard(db, feed, Recorder(), budget=1e9).sample()
    assert await db.get("credits_used") == pytest.approx(CREDITS_PER_MB)


async def test_counter_resets_on_a_new_billing_month(db):
    await db.set("credit_month", "1999-01")
    await db.set("credits_used", 900_000.0)
    guard = BudgetGuard(db, FakeFeed(), Recorder(), budget=1000)
    assert await guard.usage() == 0.0
    assert await db.get("credit_month") == datetime.now().strftime("%Y-%m")


async def test_guard_trips_and_disables_fallback(db):
    notify = Recorder()
    feed = FakeFeed(bytes_received=100_000_000, using_fallback=True)
    guard = BudgetGuard(db, feed, notify, budget=100)

    await guard.check()

    assert guard.tripped
    assert feed.fallback_url == ""
    assert feed.using_fallback is False
    assert "credit budget" in notify.messages[0].lower()


async def test_guard_announces_only_once(db):
    notify = Recorder()
    feed = FakeFeed(bytes_received=100_000_000, using_fallback=True)
    guard = BudgetGuard(db, feed, notify, budget=100)
    await guard.check()
    await guard.check()
    assert len(notify.messages) == 1


async def test_guard_stays_quiet_under_budget(db):
    notify = Recorder()
    feed = FakeFeed(bytes_received=1_000_000, using_fallback=True)
    guard = BudgetGuard(db, feed, notify, budget=1e6)
    await guard.check()
    assert notify.messages == []
    assert feed.fallback_url == "wss://fallback"


# --- health monitor ---------------------------------------------------------


async def test_healthy_feed_says_nothing():
    notify = Recorder()
    monitor = HealthMonitor(FakeFeed(connected=True), notify)
    await monitor.check()
    assert notify.messages == []


async def test_brief_outage_is_not_announced():
    """Reconnects take seconds; announcing every blip would be noise."""
    notify = Recorder()
    monitor = HealthMonitor(FakeFeed(connected=False), notify, down_after=180)
    await monitor.check()  # starts the clock
    await monitor.check()
    assert notify.messages == []


async def test_sustained_outage_is_announced():
    notify = Recorder()
    feed = FakeFeed(connected=False)
    monitor = HealthMonitor(feed, notify, down_after=60)
    await monitor.check()
    monitor.down_since = time.time() - 120
    await monitor.check()
    assert len(notify.messages) == 1
    assert "feed down" in notify.messages[0].lower()


async def test_outage_announced_once_not_repeatedly():
    notify = Recorder()
    monitor = HealthMonitor(FakeFeed(connected=False), notify, down_after=0)
    monitor.down_since = time.time() - 10
    await monitor.check()
    await monitor.check()
    await monitor.check()
    assert len(notify.messages) == 1


async def test_recovery_is_announced():
    notify = Recorder()
    feed = FakeFeed(connected=False)
    monitor = HealthMonitor(feed, notify, down_after=0)
    monitor.down_since = time.time() - 300
    await monitor.check()

    feed.connected = True
    await monitor.check()

    assert len(notify.messages) == 2
    assert "recovered" in notify.messages[1].lower()


async def test_recovery_without_a_reported_outage_is_silent():
    notify = Recorder()
    monitor = HealthMonitor(FakeFeed(connected=True), notify)
    await monitor.check()
    await monitor.check()
    assert notify.messages == []


async def test_outage_is_silent_when_nothing_is_watched(db):
    """An empty watchlist means no alerts are being missed, so no noise."""
    notify = Recorder()
    monitor = HealthMonitor(FakeFeed(connected=False), notify, down_after=0, db=db)
    monitor.down_since = time.time() - 300
    await monitor.check()
    assert notify.messages == []


async def test_recovery_is_silent_when_the_outage_was(db):
    notify = Recorder()
    feed = FakeFeed(connected=False)
    monitor = HealthMonitor(feed, notify, down_after=0, db=db)
    monitor.down_since = time.time() - 300
    await monitor.check()

    feed.connected = True
    await monitor.check()
    assert notify.messages == []


async def test_outage_is_announced_once_a_coin_is_watched(db):
    from dipbot.models import Tier

    await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    notify = Recorder()
    monitor = HealthMonitor(FakeFeed(connected=False), notify, down_after=0, db=db)
    monitor.down_since = time.time() - 300
    await monitor.check()
    assert len(notify.messages) == 1


# --- daily summary ----------------------------------------------------------


def summary(**kw) -> SummaryData:
    base = dict(
        alerts=7, tokens=3, realtime=2, feed_uptime_h=12.5, megabytes=41.2,
        reconnects=1, dex_errors=0, top=[("CATE", 4), ("RIGHT", 3)], credits_used=0.0,
    )
    base.update(kw)
    return SummaryData(**base)


def test_summary_includes_headline_numbers():
    text = render_summary(summary())
    assert "7" in text and "41.2 MB" in text and "$CATE — 4" in text


def test_summary_says_so_when_nothing_fired():
    text = render_summary(summary(alerts=0, top=[]))
    assert "No dips crossed the thresholds" in text


def test_summary_hides_credits_when_none_spent():
    assert "Credits" not in render_summary(summary(credits_used=0))


def test_summary_shows_credits_when_spent():
    assert "12,000" in render_summary(summary(credits_used=12_000))


def test_summary_hides_dex_errors_when_clean():
    assert "Dex errors" not in render_summary(summary(dex_errors=0))


async def test_summary_collects_from_the_database(db):
    from dipbot.models import Tier

    await db.add_token("mint1", "pair1", "CATE", Tier.REALTIME)
    now = time.time()
    await db.record_alert("mint1", "1m", 12.0, 1.0, 1.2, now)
    await db.record_alert("mint1", "10s", 9.0, 1.0, 1.1, now)

    class FakeDex:
        error_count = 0

    data = await DailySummary(db, FakeFeed(), FakeDex(), Recorder(), "09:00").collect()
    assert data.alerts == 2
    assert data.tokens == 1
    assert data.top == [("CATE", 2)]


async def test_summary_posts_to_the_channel(db):
    class FakeDex:
        error_count = 0

    notify = Recorder()
    await DailySummary(db, FakeFeed(), FakeDex(), notify, "09:00").post()
    assert "Daily summary" in notify.messages[0]


async def test_scheduled_summary_is_skipped_when_nothing_is_watched(db):
    """Nothing tracked, nothing to summarise - the channel stays quiet."""
    from dipbot.models import Tier
    from dipbot.ops import watching

    assert await watching(db) is False

    await db.add_token("mint1", "pair1", "CATE", Tier.POLLED)
    assert await watching(db) is True


# --- scheduling -------------------------------------------------------------


def test_seconds_until_later_today():
    now = datetime(2026, 8, 25, 8, 0, 0)
    assert seconds_until("09:00", now) == 3600


def test_seconds_until_rolls_to_tomorrow():
    now = datetime(2026, 8, 25, 10, 0, 0)
    assert seconds_until("09:00", now) == 23 * 3600


def test_seconds_until_handles_exact_match():
    """At exactly the target time, wait for tomorrow rather than firing twice."""
    now = datetime(2026, 8, 25, 9, 0, 0)
    assert seconds_until("09:00", now) == 86_400


def test_seconds_until_accepts_hour_only():
    now = datetime(2026, 8, 25, 8, 30, 0)
    assert seconds_until("09", now) == 1800
