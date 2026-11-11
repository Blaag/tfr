from __future__ import annotations

from tfr.reconnect import ReconnectBackoff, ReconnectPolicy


def test_reconnect_backoff_is_exponential_capped_jittered_and_window_bounded() -> None:
    now = 100.0
    backoff = ReconnectBackoff(
        ReconnectPolicy(
            initial_delay_seconds=1,
            max_delay_seconds=4,
            window_seconds=60,
            stable_reset_seconds=5,
        ),
        clock=lambda: now,
        jitter=lambda low, high: (low + high) / 2,
    )

    assert [backoff.next().delay_seconds for _ in range(5)] == [1, 2, 4, 4, 4]  # type: ignore[union-attr]
    now = 159.5
    final = backoff.next()
    assert final is not None and final.delay_seconds == 0.5
    now = 160.0
    assert backoff.next() is None
    assert backoff.status()["exhausted"] is True


def test_reconnect_backoff_jitter_stays_within_twenty_percent() -> None:
    values = iter((0.8, 2.4))
    backoff = ReconnectBackoff(
        ReconnectPolicy(window_seconds=60),
        clock=lambda: 0,
        jitter=lambda _low, _high: next(values),
    )

    assert backoff.next().delay_seconds == 0.8  # type: ignore[union-attr]
    assert backoff.next().delay_seconds == 2.4  # type: ignore[union-attr]


def test_default_reconnect_progression_uses_aggressive_human_scale_delays() -> None:
    backoff = ReconnectBackoff(
        ReconnectPolicy(),
        clock=lambda: 0,
        jitter=lambda low, high: (low + high) / 2,
    )

    assert [backoff.next().delay_seconds for _ in range(9)] == [  # type: ignore[union-attr]
        1,
        2,
        4,
        8,
        16,
        30,
        60,
        120,
        120,
    ]


def test_only_stable_connection_resets_backoff() -> None:
    backoff = ReconnectBackoff(
        ReconnectPolicy(window_seconds=60, stable_reset_seconds=5),
        clock=lambda: 0,
        jitter=lambda low, _high: low,
    )
    backoff.next()
    backoff.next()

    assert backoff.observe_connection(4.9) is False
    assert backoff.attempts == 2
    assert backoff.observe_connection(5.0) is True
    assert backoff.attempts == 0
    assert backoff.started_at is None
