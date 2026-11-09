"""Zeitsteuerung pro Zeile.

Drei Zustaende:

  ACTIVE    Zeitfenster offen, alles laeuft.
  EXTENDED  Fenster ist zu, aber es war zuletzt Verkehr. Es laeuft ganz
            normal weiter, auch neue Durchgaenge - die Runde soll nicht
            mitten drin zerbrechen. Erst wenn nach dem letzten Talker-End
            'grace_minutes' wirklich Ruhe ist, geht die Zeile zu.
  INACTIVE  Aus.

shutdown_mode = hard ueberspringt EXTENDED komplett und schaltet um
Punkt Fensterende ab.

Zaehlt wird der Verkehr aus beiden Richtungen - SVX-Talker genauso wie
GROUP_TX von der TETRA-Seite.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

from .config import Route, SHUTDOWN_HARD, Schedule, Window

STATE_ACTIVE = "active"
STATE_EXTENDED = "extended"
STATE_INACTIVE = "inactive"


def _parse_hhmm(value: str) -> tuple[int, int]:
    hour, minute = value.split(":")
    return int(hour), int(minute)


def _window_bounds(day: datetime, window: Window) -> tuple[datetime, datetime]:
    """Liefert Start/Ende eines Fensters an einem konkreten Tag.

    Ende <= Start heisst: laeuft ueber Mitternacht.
    """
    start_h, start_m = _parse_hhmm(window.start)
    end_h, end_m = _parse_hhmm(window.end)
    start = day.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    end = day.replace(hour=end_h, minute=end_m, second=0, microsecond=0)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def active_window(schedule: Schedule, now: datetime) -> Optional[tuple[datetime, datetime]]:
    """Das gerade laufende Fenster, oder None."""
    days = schedule.day_numbers
    for offset in (0, 1):          # heute und gestern (Mitternachtsfenster)
        day = (now - timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if day.weekday() not in days:
            continue
        for window in schedule.windows:
            start, end = _window_bounds(day, window)
            if start <= now < end:
                return start, end
    return None


def next_window_start(schedule: Schedule, now: datetime) -> Optional[datetime]:
    """Naechster Fensterbeginn innerhalb der kommenden Woche."""
    days = schedule.day_numbers
    best: Optional[datetime] = None
    for offset in range(0, 8):
        day = (now + timedelta(days=offset)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        if day.weekday() not in days:
            continue
        for window in schedule.windows:
            start, _ = _window_bounds(day, window)
            if start > now and (best is None or start < best):
                best = start
    return best


@dataclass
class SchedulerStatus:
    state: str
    reason: str
    active: bool
    manual: Optional[bool]
    window_end: Optional[datetime]
    grace_until: Optional[datetime]
    talkers: int


class RouteScheduler:
    def __init__(self, route: Route, tz: str = "Europe/Vienna"):
        self.route = route
        self.tz = ZoneInfo(tz)
        self.state = STATE_INACTIVE
        self.manual: Optional[bool] = None       # None = Automatik
        self._manual_until: Optional[datetime] = None
        self._talkers = 0
        self._last_traffic_end: Optional[datetime] = None
        self._extension_started: Optional[datetime] = None
        self._window_end: Optional[datetime] = None
        self._reason = "start"

    # ------------------------------------------------------------- Eingaben

    def now(self) -> datetime:
        return datetime.now(self.tz)

    def note_traffic_start(self, now: Optional[datetime] = None) -> None:
        self._talkers += 1
        self._last_traffic_end = None

    def note_traffic_end(self, now: Optional[datetime] = None) -> None:
        self._talkers = max(0, self._talkers - 1)
        if self._talkers == 0:
            self._last_traffic_end = now or self.now()

    def set_manual(self, value: Optional[bool], now: Optional[datetime] = None) -> None:
        """True/False = Handbetrieb, None = zurueck auf Automatik.

        Handbetrieb gilt bis zum naechsten Fensterwechsel, sonst wuerde der
        Scheduler eine Minute spaeter drueberfahren.
        """
        now = now or self.now()
        self.manual = value
        if value is None:
            self._manual_until = None
            return
        schedule = self.route.schedule
        if schedule.enabled:
            window = active_window(schedule, now)
            self._manual_until = window[1] if window else next_window_start(schedule, now)
        else:
            self._manual_until = None

    # ------------------------------------------------------------ Auswertung

    def update(self, now: Optional[datetime] = None) -> str:
        now = now or self.now()
        route = self.route
        schedule = route.schedule

        if self._manual_until is not None and now >= self._manual_until:
            self.manual = None
            self._manual_until = None

        if self.manual is not None:
            self.state = STATE_ACTIVE if self.manual else STATE_INACTIVE
            self._reason = "manuell"
            return self.state

        if not route.enabled:
            self.state = STATE_INACTIVE
            self._reason = "Zeile deaktiviert"
            return self.state

        if not schedule.enabled:
            self.state = STATE_ACTIVE
            self._reason = "kein Zeitplan"
            return self.state

        window = active_window(schedule, now)
        if window is not None:
            self.state = STATE_ACTIVE
            self._window_end = window[1]
            self._extension_started = None
            self._reason = "Fenster offen"
            return self.state

        # Fenster ist zu.
        if self.state == STATE_ACTIVE:
            if route.shutdown_mode == SHUTDOWN_HARD:
                self.state = STATE_INACTIVE
                self._reason = "Fensterende (hart)"
                return self.state
            self._extension_started = self._window_end or now
            self.state = STATE_EXTENDED
            self._reason = "Nachlauf"

        if self.state == STATE_EXTENDED:
            started = self._extension_started or now
            cap = route.max_extend_minutes
            if cap and now - started >= timedelta(minutes=cap):
                self.state = STATE_INACTIVE
                self._reason = f"Nachlauf-Obergrenze ({cap:g} min)"
                return self.state
            if self._talkers > 0:
                self._reason = "Nachlauf, Verkehr laeuft"
                return self.state
            reference = self._last_traffic_end or started
            if now - reference >= timedelta(minutes=route.grace_minutes):
                self.state = STATE_INACTIVE
                self._reason = f"{route.grace_minutes:g} min Ruhe"
            else:
                self._reason = "Nachlauf, warte auf Ruhe"
            return self.state

        self.state = STATE_INACTIVE
        self._reason = "ausserhalb Zeitfenster"
        return self.state

    def is_active(self, now: Optional[datetime] = None) -> bool:
        return self.update(now) in (STATE_ACTIVE, STATE_EXTENDED)

    def grace_until(self, now: Optional[datetime] = None) -> Optional[datetime]:
        if self.state != STATE_EXTENDED or self._talkers > 0:
            return None
        now = now or self.now()
        reference = self._last_traffic_end or self._extension_started or now
        return reference + timedelta(minutes=self.route.grace_minutes)

    def status(self, now: Optional[datetime] = None) -> SchedulerStatus:
        now = now or self.now()
        self.update(now)
        return SchedulerStatus(
            state=self.state,
            reason=self._reason,
            active=self.state in (STATE_ACTIVE, STATE_EXTENDED),
            manual=self.manual,
            window_end=self._window_end,
            grace_until=self.grace_until(now),
            talkers=self._talkers,
        )
