"""Optional synchronisation of the boiler clock with the host clock."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, tzinfo

from diematic.core.poller import RegisterBus
from diematic.core.state import StateStore
from diematic.modbus.rtu import ModbusError
from diematic.registers.loader import RegisterMap

logger = logging.getLogger(__name__)

HOUR, MINUTE, WEEKDAY, DAY, MONTH, YEAR = (
    "clock_hour",
    "clock_minute",
    "clock_weekday",
    "clock_day",
    "clock_month",
    "clock_year",
)
CLOCK_KEYS = (HOUR, MINUTE, WEEKDAY, DAY, MONTH, YEAR)


class ClockSync:
    """Sets the boiler clock once its drift has persisted over several consecutive checks."""

    def __init__(
        self,
        bus: RegisterBus,
        regmap: RegisterMap,
        state: StateStore,
        timezone: tzinfo,
        *,
        tolerance: float = 60.0,
        confirmations: int = 6,
        interval: float = 60.0,
        now: Callable[[tzinfo], datetime] = datetime.now,
    ) -> None:
        self._bus = bus
        self._regmap = regmap
        self._state = state
        self._tz = timezone
        self._tolerance = tolerance
        self._confirmations = confirmations
        self._interval = interval
        self._now = now
        self._drifting = 0

    @staticmethod
    def supported(regmap: RegisterMap) -> bool:
        return all(key in regmap and regmap.get(key).width == 1 for key in CLOCK_KEYS)

    def boiler_time(self) -> datetime | None:
        words = [self._state.raw(key) for key in CLOCK_KEYS]
        if any(w is None for w in words):
            return None
        hour, minute, _, day, month, year = (w[0] for w in words if w is not None)
        try:
            return datetime(2000 + year, month, day, hour, minute, tzinfo=self._tz)
        except ValueError:
            return None

    def drift(self) -> float | None:
        """Seconds the boiler clock is ahead of the host clock (None while unknown)."""
        boiler = self.boiler_time()
        return None if boiler is None else (boiler - self._now(self._tz)).total_seconds()

    async def check(self) -> bool:
        """Run one check; returns True when the clock was set."""
        drift = self.drift()
        if drift is None or abs(drift) < self._tolerance:
            self._drifting = 0
            return False
        self._drifting += 1
        if self._drifting < self._confirmations:
            return False
        self._drifting = 0
        await self._set(self._now(self._tz))
        return True

    async def _set(self, moment: datetime) -> None:
        raw = {
            HOUR: moment.hour,
            MINUTE: moment.minute,
            WEEKDAY: moment.isoweekday(),
            DAY: moment.day,
            MONTH: moment.month,
            YEAR: moment.year % 100,
        }
        logger.info("setting the boiler clock to %s", moment.isoformat(timespec="minutes"))
        for key in CLOCK_KEYS:
            await self._bus.write_registers(
                self._regmap.get(key).address, [raw[key]], max_wait=60.0
            )

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self._interval)
            try:
                await self.check()
            except ModbusError as exc:
                logger.warning("could not set the boiler clock: %s", exc)
