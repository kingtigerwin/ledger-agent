from datetime import UTC, date, datetime

import pytest

from ledger_agent.clock import InvalidTimeZone, today_in


class TestGivenAnInstantNearMidnightUtc:
    # 2026-10-27 13:30 UTC is already the 28th in Sydney (UTC+11 in daylight saving) but still the 27th in London.
    now = datetime(2026, 10, 27, 13, 30, tzinfo=UTC)

    def test_it_is_tomorrow_in_sydney(self) -> None:
        assert today_in("Australia/Sydney", self.now) == date(2026, 10, 28)

    def test_it_is_still_today_in_london(self) -> None:
        assert today_in("Europe/London", self.now) == date(2026, 10, 27)


class TestGivenAnUnknownTimeZone:
    @pytest.mark.parametrize("name", ["Mars/Olympus_Mons", "", "../../etc/passwd"])
    def test_it_is_rejected(self, name: str) -> None:
        with pytest.raises(InvalidTimeZone):
            today_in(name, datetime(2026, 10, 27, tzinfo=UTC))
