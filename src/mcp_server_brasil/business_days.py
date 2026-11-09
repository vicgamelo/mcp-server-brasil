"""Business-day arithmetic over the Brazilian national holiday calendar."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, timedelta


def business_days_between(start: date, end: date, holidays: Iterable[date]) -> list[date]:
    """Return the business days in the closed interval ``[start, end]``.

    Weekends and the given holidays are excluded. The interval is normalized,
    so ``start > end`` behaves like ``end..start``.
    """
    if start > end:
        start, end = end, start
    blocked = set(holidays)
    days: list[date] = []
    current = start
    while current <= end:
        if current.weekday() < 5 and current not in blocked:
            days.append(current)
        current += timedelta(days=1)
    return days


def years_in_range(start: date, end: date) -> range:
    low, high = sorted((start.year, end.year))
    return range(low, high + 1)
