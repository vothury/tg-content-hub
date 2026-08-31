from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.workers.reader import SourceSnapshot, _select_fresh


def _snap(**overrides):
    base = dict(
        id=1, username="x", telegram_id=None, last_read_message_id=None,
        poll_interval_sec=120, backfill_limit=20, last_read_at=None,
        target_channel_id=None, fresh_window_min=60, fallback_count=2,
        fallback_max_age_hours=48,
    )
    base.update(overrides)
    return SourceSnapshot(**base)


def _msg(mid: int, minutes_ago: float):
    return SimpleNamespace(id=mid, date=datetime.now(timezone.utc) - timedelta(minutes=minutes_ago))


def test_fresh_window_taken() -> None:
    msgs = [_msg(1, 120), _msg(2, 30), _msg(3, 10)]
    got = _select_fresh(msgs, _snap())
    assert [m.id for m in got] == [2, 3]


def test_fallback_when_no_fresh() -> None:
    msgs = [_msg(1, 5000), _msg(2, 3000), _msg(3, 1000), _msg(4, 500)]
    got = _select_fresh(msgs, _snap(fresh_window_min=60, fallback_count=2, fallback_max_age_hours=48))
    assert [m.id for m in got] == [3, 4]


def test_too_old_even_for_fallback() -> None:
    msgs = [_msg(1, 10000), _msg(2, 9000)]
    got = _select_fresh(msgs, _snap())
    assert got == []


def test_fallback_disabled() -> None:
    msgs = [_msg(1, 3000)]
    got = _select_fresh(msgs, _snap(fallback_count=0))
    assert got == []