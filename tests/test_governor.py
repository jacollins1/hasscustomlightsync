"""Governor: rate cap, coalescing, staleness, group spacing, AIMD."""

from __future__ import annotations

from custom_components.music_light_sync.engine import GROUP, PRIO_BEAT, PRIO_COLOR, Command
from custom_components.music_light_sync.governor import Governor


def cmd(target: int, effect: int, level: int | None = 100, prio: int = PRIO_BEAT) -> Command:
    return Command(effect, target, level, None if level is not None else (255, 0, 0), 5, prio)


def test_token_bucket_caps_rate() -> None:
    gov = Governor(rate_max=10, burst=5, max_inflight=100)
    sent = 0
    t = 0.0
    while t < 10:
        gov.offer([cmd(i, int(t * 1e6)) for i in range(5)])
        sent += len(gov.take(t, int(t * 1e6)))
        for _ in range(gov.inflight):
            gov.on_result(t, 0.05, True)
        t += 0.05
    assert 95 <= sent <= 106  # 10/s plus the initial burst


def test_newer_command_replaces_unsent_one() -> None:
    gov = Governor(rate_max=10, burst=0)
    gov.offer([cmd(0, 100, level=10)])
    gov.offer([cmd(0, 200, level=20)])
    gov._tokens = 1  # noqa: SLF001
    out = gov.take(0.0, 300)
    assert [c.level for c in out] == [20]  # both due: only the latest matters
    assert gov.stats.coalesced == 1


def test_flash_and_its_fade_are_both_kept_when_not_yet_due() -> None:
    gov = Governor(rate_max=20, burst=10)
    gov.offer([cmd(0, 1_000_000, level=254), cmd(0, 1_090_000, level=40)])
    first = gov.take(0.0, 1_000_000)
    assert [c.level for c in first] == [254]
    gov.on_result(0.05, 0.05, True)
    second = gov.take(0.1, 1_090_000)
    assert [c.level for c in second] == [40]
    assert gov.stats.coalesced == 0


def test_same_moment_replaces() -> None:
    gov = Governor(rate_max=20, burst=10)
    gov.offer([cmd(0, 500, level=40), cmd(0, 500, level=254)])
    assert [c.level for c in gov.take(0.0, 500)] == [254]


def test_stale_commands_dropped_and_future_ones_held() -> None:
    gov = Governor(rate_max=10, burst=5)
    gov.offer([cmd(0, 0), cmd(1, 2_000_000)])
    out = gov.take(0.0, 1_000_000)
    assert out == []
    assert gov.stats.dropped_stale == 1
    assert len(gov.take(0.1, 2_000_000)) == 1


def test_priority_order_beats_before_colors() -> None:
    gov = Governor(rate_max=10, burst=1)
    gov.offer([cmd(0, 100, level=None, prio=PRIO_COLOR), cmd(1, 100)])
    out = gov.take(0.0, 100)
    assert out[0].level is not None


def test_group_frames_spaced() -> None:
    gov = Governor(rate_max=50, burst=10, group_min_interval_s=2.0)
    gov.offer([cmd(GROUP, 0, level=None)])
    assert len(gov.take(0.0, 0)) == 1
    gov.offer([cmd(GROUP, 1_000_000, level=None)])
    assert gov.take(1.0, 1_000_000) == []
    assert gov.take(2.05, 1_050_000) != []


def test_aimd_backs_off_and_recovers() -> None:
    gov = Governor(rate_max=12, rate_min=2)
    gov.inflight = 50
    # Normal Hue/ZHA round trips (~200 ms, with burst queuing) must NOT back off.
    t = 0.0
    for i in range(200):
        gov.on_result(t, 0.15 if i % 5 else 0.42, True)
        t += 0.1
    assert gov.rate == 12
    # A congested mesh: smoothed RTT climbs well above 450 ms -> back off, once per window.
    for _ in range(20):
        gov.on_result(t, 0.9, True)
        t += 0.1
    assert 12 * 0.75**2 - 0.01 <= gov.rate < 12  # 2 s of trouble = at most 2 backoffs
    low = gov.rate
    gov.on_result(t, None, False)
    assert gov.rate <= low
    # Healthy again: climbs back to the cap.
    for _ in range(300):
        gov.on_result(t, 0.12, True)
        t += 0.1
    assert gov.rate == 12
    assert 0.05 < gov.one_way_s < 0.07


def test_inflight_limit() -> None:
    gov = Governor(rate_max=100, burst=50, max_inflight=3)
    gov.offer([cmd(i, 0) for i in range(6)])
    assert len(gov.take(0.0, 0)) == 3
    assert gov.take(0.01, 0) == []
    gov.on_result(0.02, 0.05, True)
    assert len(gov.take(0.03, 0)) == 1
