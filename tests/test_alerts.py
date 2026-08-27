"""Alert formatting and the rate-limited sender."""
from __future__ import annotations

import re
import time

import pytest

from dipbot.alerts import (
    Alert,
    AlertSender,
    fmt_age,
    fmt_usd,
    keyboard,
    market_cap_move,
    links_keyboard,
    render,
    render_added,
    trojan_url,
)
from dipbot.models import Tier
from tests.conftest import make_meta


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, "?"),
        (2_300_000_000, "$2.30B"),
        (65_000_000, "$65.00M"),
        (81_991.65, "$81.99k"),
        (1_500, "$1.50k"),
        (12.5, "$12.50"),
    ],
)
def test_fmt_usd(value, expected):
    assert fmt_usd(value) == expected


def test_fmt_usd_small_numbers_keep_precision():
    assert fmt_usd(0.0000058).startswith("$0.0000058")


@pytest.mark.parametrize(
    "minutes, expected",
    [
        (None, "?"),
        (5, "5m"),
        (59, "59m"),
        (60, "1h 0m"),
        (90, "1h 30m"),
        (1439, "23h 59m"),
        (2880, "2d"),
        (3000, "2d 2h"),
    ],
)
def test_fmt_age(minutes, expected):
    assert fmt_age(minutes) == expected


# --- rendering --------------------------------------------------------------


def alert(**kw) -> Alert:
    base = dict(
        mint="Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump",
        window="1m",
        drawdown=14.2,
        price_sol=0.00069,
        baseline_sol=0.0008,
        tier=Tier.REALTIME,
        meta=make_meta(),
        ts=time.time(),
    )
    base.update(kw)
    return Alert(**base)


def test_render_includes_key_facts():
    text = render(alert())
    assert "Cate" in text            # coin name, not the ticker
    assert "-14%" in text
    assert "in 1m" in text
    assert "Ai66LHZG9MCzg1WKdawwqduVAXpNDUuV8M3uyq5ppump" in text


def test_render_matches_the_requested_format_exactly():
    """The format was specified character for character; pin it that way."""
    a = alert(
        mint="AA33znW3bciXvrj8AUwPryVgDiasmEGxaqCwjHEWGdkd",
        window="1m", drawdown=60.0, mcap_peak=122_000.0, mcap_now=60_000.0,
        ath=300_000.0,
        meta=make_meta(name="Coin Name", volume_m5=300_000.0, volume_h24=700_000.0,
                       pair_created_at=int((time.time() - 8 * 60) * 1000)),
    )
    expected = (
        "Coin Name -60% in 1m\n"
        "\n"
        "MC: 122k -> 60k\n"
        "ATH: 300k\n"
        "Age: 8m\n"
        "Vol 5m: 300k\n"
        "Vol 24h: 700k\n"
        "\n"
        "AA33znW3bciXvrj8AUwPryVgDiasmEGxaqCwjHEWGdkd"
    )
    # the mint is wrapped in <code> so it stays tap-to-copy on mobile
    assert re.sub(r"</?code>", "", render(a)) == expected


def test_render_uses_a_plain_hyphen_and_arrow():
    """Not the typographic − and →, which is what was asked for."""
    text = render(alert(mcap_peak=122_000.0, mcap_now=60_000.0))
    assert "−" not in text
    assert "→" not in text
    assert "->" in text


def test_render_uses_the_name_and_falls_back_to_the_symbol():
    assert "Gta Buckz" in render(alert(meta=make_meta(name="Gta Buckz", symbol="b")))
    assert "ONLYSYM" in render(alert(meta=make_meta(name=None, symbol="ONLYSYM")))


def test_render_without_metadata_does_not_crash():
    text = render(alert(meta=None))
    assert "-14%" in text


def test_render_handles_unknown_liquidity(memedex):
    """MEMEDEX-style token: no liquidity figure must not break the message."""
    text = render(alert(meta=memedex))
    assert "Memedex" in text
    assert "MC:" in text  # a null liquidity must not break the message


def test_keyboard_has_chart_and_actions():
    labels = [b.text for row in keyboard(alert()).inline_keyboard for b in row]
    assert "Chart" in labels
    assert any("Mute" in x for x in labels)
    assert any("Remove" in x for x in labels)


# --- sender -----------------------------------------------------------------


class FakeBot:
    def __init__(self, fail: bool = False):
        self.sent: list[str] = []
        self.fail = fail
        self._id = 0

    async def send_message(self, chat_id, text, **kwargs):
        if self.fail:
            raise RuntimeError("telegram down")
        self.sent.append(text)
        self._id += 1
        return type("Msg", (), {"message_id": self._id})()


pytestmark_async = pytest.mark.asyncio


@pytest.mark.asyncio
async def test_send_alert_delivers():
    bot = FakeBot()
    sender = AlertSender(bot, channel_id=-100, max_per_minute=10)
    msg_id = await sender.send_alert(alert())
    assert msg_id == 1
    assert sender.sent_count == 1
    assert "Cate" in bot.sent[0]


@pytest.mark.asyncio
async def test_send_failure_is_counted_not_raised():
    sender = AlertSender(FakeBot(fail=True), channel_id=-100)
    assert await sender.send_alert(alert()) is None
    assert sender.error_count == 1


@pytest.mark.asyncio
async def test_rate_limit_queues_overflow():
    bot = FakeBot()
    sender = AlertSender(bot, channel_id=-100, max_per_minute=2)
    for _ in range(5):
        await sender.send_alert(alert())
    assert len(bot.sent) == 2
    assert len(sender._pending) == 3


@pytest.mark.asyncio
async def test_overflow_is_coalesced_not_dropped():
    """Losing a dip alert silently is worse than batching it."""
    bot = FakeBot()
    sender = AlertSender(bot, channel_id=-100, max_per_minute=2)
    for _ in range(5):
        await sender.send_alert(alert())

    sender.max_per_minute = 10  # budget frees up
    await sender.flush_digest()

    digest = bot.sent[-1]
    assert "3 more dips" in digest
    assert sender._pending == []


@pytest.mark.asyncio
async def test_flush_digest_noop_when_nothing_pending():
    sender = AlertSender(FakeBot(), channel_id=-100)
    assert await sender.flush_digest() is None


@pytest.mark.asyncio
async def test_budget_recovers_after_window():
    bot = FakeBot()
    sender = AlertSender(bot, channel_id=-100, max_per_minute=1)
    await sender.send_alert(alert())
    assert len(bot.sent) == 1

    # Pretend the earlier send was over a minute ago.
    sender._sent_times = [time.time() - 61]
    await sender.send_alert(alert())
    assert len(bot.sent) == 2


# --- market cap presentation (client request) -------------------------------


def test_market_cap_move_uses_observed_values():
    """Both ends must be caps the poller really saw."""
    a = alert(mcap_peak=100_000.0, mcap_now=50_000.0)
    assert market_cap_move(a) == (100_000.0, 50_000.0)


def test_market_cap_move_is_never_derived_from_the_price_ratio():
    """The regression: a derived cap reported a fall from $122.93k on a coin
    whose real high that minute was $56.39k."""
    a = alert(drawdown=46.4, price_sol=3.515e-05, baseline_sol=6.559e-05,
              meta=make_meta(market_cap=65_880.0), mcap_peak=None, mcap_now=None)
    assert market_cap_move(a) is None
    assert "122" not in render(a)
    assert "->" not in render(a)


def test_market_cap_move_is_none_without_observations():
    assert market_cap_move(alert(mcap_peak=None, mcap_now=50_000.0)) is None
    assert market_cap_move(alert(mcap_peak=100_000.0, mcap_now=None)) is None


def test_market_cap_move_is_none_when_no_fall_was_observed():
    """The poller may simply have missed the peak; don't invent one."""
    assert market_cap_move(alert(mcap_peak=50_000.0, mcap_now=50_000.0)) is None
    assert market_cap_move(alert(mcap_peak=40_000.0, mcap_now=50_000.0)) is None


def test_render_leads_with_market_cap():
    a = alert(mcap_peak=100_000.0, mcap_now=50_000.0)
    body = render(a).split("\n")
    first = next(line for line in body[2:] if line.strip())
    assert "MC:" in first
    assert "->" in first


def test_render_shows_the_observed_cap_transition():
    text = render(alert(mcap_peak=100_000.0, mcap_now=50_000.0))
    assert "MC: 100k -> 50k" in text


def test_render_falls_back_to_the_current_cap_alone():
    """No observed peak: show what we know, not a fabricated arrow."""
    text = render(alert(mcap_peak=None, mcap_now=50_000.0))
    assert "MC: 50k" in text
    assert "->" not in text


def test_observed_cap_percentage_is_independent_of_the_headline():
    """The headline uses the fine-grained price feed; the cap line uses polled
    observations, so they can legitimately differ."""
    text = render(alert(drawdown=46.4, mcap_peak=100_000.0, mcap_now=80_000.0))
    assert "-46%" in text                           # headline, from the price feed
    assert "MC: 100k -> 80k" in text                # cap line, from observations


def test_render_shows_the_tracked_high():
    text = render(alert(meta=make_meta(ath_market_cap=180_000.0)))
    assert "ATH: 180k" in text


def test_render_omits_ath_when_never_recorded():
    assert "ATH" not in render(alert(meta=make_meta(ath_market_cap=None)))


def test_render_survives_missing_market_cap():
    text = render(alert(meta=make_meta(market_cap=None), mcap_now=None, mcap_peak=None))
    assert "Cate" in text
    assert "MC:" not in text


# --- trojan button ----------------------------------------------------------


def test_trojan_button_replaces_solscan():
    labels = [b.text for row in keyboard(alert()).inline_keyboard for b in row]
    assert "Trojan" in labels
    assert "Solscan" not in labels





def test_trojan_link_targets_the_mint_not_the_pair():
    a = alert()
    urls = [b.url for row in keyboard(a).inline_keyboard for b in row if b.url]
    trojan = next(u for u in urls if "trojanbot" in u)
    assert a.mint in trojan
    assert a.meta.pair_address not in trojan


@pytest.mark.parametrize(
    "value, expected",
    [
        (0, "$0"),
        (0.0, "$0"),
        (0.00000461, "$0.00000461"),
        (0.5, "$0.5"),
        (0.10000000, "$0.1"),
        (1.0, "$1.00"),
    ],
)
def test_fmt_usd_never_leaves_a_dangling_decimal_point(value, expected):
    """A zero volume rendered as "$0." in a live alert."""
    assert fmt_usd(value) == expected


def test_fmt_usd_output_is_always_well_formed():
    for value in (0, 1e-9, 0.000001, 0.5, 1, 999.999, 1500, 1e6, 1e9, 2.5e9):
        text = fmt_usd(value)
        assert not text.endswith("."), text
        assert text.startswith("$")


def test_render_omits_the_per_token_price():
    """Market cap carries the same information at a readable scale."""
    text = render(alert(meta=make_meta(price_usd=0.00000461)))
    assert "Price" not in text
    assert "0.00000461" not in text


def test_render_without_metadata_says_so():
    """Better to state the gap than render a bare headline."""
    text = render(alert(meta=None))
    assert "no market data" in text


# --- trojan deep link -------------------------------------------------------
# Trojan parses `?start=r-<code>-<mint>`. A bare mint was tested against the
# live bot and did not open the coin, so the prefix is format, not decoration.


def test_trojan_url_matches_the_format_trojan_expects():
    mint = "4iKit2Ur16idvCkDKXSTT6iYBQDyrFrkSCjca1gzPmMS"
    assert trojan_url(mint, bot="solana_trojanbot", ref="exampleref01") == (
        f"https://t.me/solana_trojanbot?start=r-exampleref01-{mint}"
    )


def test_trojan_url_uses_the_mint_not_the_pair():
    a = alert()
    url = trojan_url(a.mint, ref="code")
    assert url.endswith(a.mint)
    assert a.meta.pair_address not in url


def test_trojan_url_without_a_code_omits_the_prefix():
    """Degrades rather than emitting a malformed `r--<mint>`."""
    url = trojan_url("MINT123", bot="b", ref="")
    assert url == "https://t.me/b?start=MINT123"
    assert "r-" not in url.split("start=")[1]


def test_trojan_url_never_emits_an_empty_ref_segment():
    assert "r--" not in trojan_url("MINT123", bot="b", ref="")


def test_trojan_bot_is_configurable():
    assert "odysseus_trojanbot" in trojan_url("M", bot="odysseus_trojanbot", ref="x")
    assert "solana_trojanbot" in trojan_url("M", bot="solana_trojanbot", ref="x")


def test_trojan_bot_accepts_an_at_prefix():
    assert trojan_url("M", bot="@solana_trojanbot", ref="x").startswith(
        "https://t.me/solana_trojanbot"
    )


def test_trojan_button_uses_module_defaults(monkeypatch):
    import dipbot.alerts as mod

    monkeypatch.setattr(mod, "TROJAN_BOT", "solana_trojanbot")
    monkeypatch.setattr(mod, "TROJAN_REF", "mycode")
    a = alert()
    url = next(
        b.url for row in keyboard(a).inline_keyboard for b in row
        if b.url and "trojanbot" in b.url
    )
    assert url == f"https://t.me/solana_trojanbot?start=r-mycode-{a.mint}"


# --- the message posted when someone adds a coin ----------------------------


def added_meta(**kw):
    base = dict(
        name="dog in cats world", symbol="BARK", market_cap=171_394.0,
        ath_market_cap=171_394.0, volume_m5=14_000.0, volume_h24=815_000.0,
        pair_created_at=int((time.time() - 62 * 60) * 1000),
        mint="BTKfTUMRYsS5W5RXfbtMcgJf9fqoz1rhRcJtESyPpump",
    )
    base.update(kw)
    return make_meta(**base)


def test_added_message_matches_the_alert_layout():
    text = re.sub(r"</?code>", "", render_added(added_meta(), "@jman"))
    assert text == (
        "dog in cats world added by @jman\n"
        "\n"
        "MC: 171k\n"
        "ATH: 171k\n"
        "Age: 1h 2m\n"
        "Vol 5m: 14k\n"
        "Vol 24h: 815k\n"
        "\n"
        "BTKfTUMRYsS5W5RXfbtMcgJf9fqoz1rhRcJtESyPpump"
    )


def test_added_message_credits_the_person():
    assert "@jman" in render_added(added_meta(), "@jman")


def test_added_message_drops_dex_and_liquidity():
    """Both were called out as clutter."""
    text = render_added(added_meta(), "@x")
    assert "Dex" not in text
    assert "Liq" not in text
    assert "pumpswap" not in text


def test_added_message_survives_missing_fields():
    text = render_added(added_meta(market_cap=None, ath_market_cap=None), "@x")
    assert "MC:" not in text
    assert "ATH:" not in text
    assert "Vol 24h:" in text


def test_added_message_falls_back_to_symbol_then_mint():
    assert "BARK" in render_added(added_meta(name=None), "@x")
    assert "BTKfTU" in render_added(added_meta(name=None, symbol=None), "@x")


def test_links_keyboard_is_shared_with_alerts():
    from dipbot.alerts import links_keyboard

    meta = added_meta()
    labels = [b.text for row in links_keyboard(meta.mint, meta.pair_address).inline_keyboard for b in row]
    assert labels == ["Chart", "pump.fun", "Trojan"]
