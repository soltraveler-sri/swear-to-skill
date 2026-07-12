"""Pin the shared timestamp semantics that six pipeline modules rely on."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from s2s.timeutils import as_utc, parse_timestamp, utc_now_iso


def test_parse_timestamp_normalizes_offsets_to_utc() -> None:
    parsed = parse_timestamp("2026-07-05T20:00:00-07:00")
    assert parsed == datetime(2026, 7, 6, 3, 0, tzinfo=timezone.utc)
    # Normalization is deliberate: date bucketing (meter weeks, audit windows)
    # uses the UTC calendar, not each timestamp's recorded local offset.
    assert parsed.date().isoformat() == "2026-07-06"


def test_parse_timestamp_accepts_z_suffix_and_naive_values() -> None:
    aware = parse_timestamp("2026-07-05T12:00:00Z")
    naive = parse_timestamp("2026-07-05T12:00:00")
    assert aware == naive == datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)


def test_parse_timestamp_returns_none_for_junk() -> None:
    for value in (None, "", 12345, "not-a-timestamp", ["2026-07-05"]):
        assert parse_timestamp(value) is None


def test_mixed_naive_and_aware_timestamps_stay_comparable() -> None:
    # Before consolidation, adapter and meter copies could hand naive and
    # aware datetimes to the same comparison and raise TypeError.
    values = [parse_timestamp("2026-07-05T12:00:00"), parse_timestamp("2026-07-05T14:00:00Z")]
    assert max(values) == datetime(2026, 7, 5, 14, 0, tzinfo=timezone.utc)


def test_as_utc_coerces_naive_and_converts_aware() -> None:
    naive = datetime(2026, 7, 5, 12, 0)
    assert as_utc(naive) == datetime(2026, 7, 5, 12, 0, tzinfo=timezone.utc)
    aware = datetime(2026, 7, 5, 12, 0, tzinfo=timezone(timedelta(hours=-7)))
    assert as_utc(aware) == datetime(2026, 7, 5, 19, 0, tzinfo=timezone.utc)


def test_utc_now_iso_is_parseable_and_aware() -> None:
    parsed = parse_timestamp(utc_now_iso())
    assert parsed is not None and parsed.tzinfo is not None
