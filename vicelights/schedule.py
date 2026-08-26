"""What the sign should say right now, built from the week's calendar.

The panel used to cycle a fixed list of hand-typed messages. This turns that
list into something that changes on its own as the week goes on: what we serve
today, a look at tomorrow, the current temperature, and -- while an event is
actually happening -- the shout for it.

Nothing here talks to the panel. It only decides the *text*; ``MatrixRunner``
still does the sending, the paging and the rotation, so a schedule message and
a hand-typed one travel the exact same road. The runner asks this module for a
list of messages, gets back the same message dicts it already knows how to
show, and cycles them. Each slot has a **stable id** across regenerations
(``sched-vice`` is always ``sched-vice``), so the rotation advances cleanly
minute to minute even as the words inside a slot change.

Two things this needs that a fixed list did not:

* **the date**, from the timekeeper. Without a set clock there is no "today",
  so those lines are dropped rather than guessed -- VICE, the all-day
  offerings and the temperature still show.
* **the temperature**, from a sampler that reads the sensor on its own slow
  thread. A missing or stale reading drops the temperature line; it is never
  faked from an old value.

The calendar below is this camp's Burning Man 2026 week, transcribed from the
schedule sheet. Times not on the sheet (how long each thing runs) are
assumptions, marked as such -- edit ``EVENTS`` to change them.
"""

from __future__ import annotations

import datetime as dt
import logging

from . import matrix as matrix_module

log = logging.getLogger("vicelights.schedule")


class Event:
    """One thing we do, on one day.

    ``start``/``end`` are "HH:MM" 24-hour, or None for something that runs all
    day (the nail spa). ``offering`` is the short form for the day summary;
    ``title`` is the longer shout used while it is happening. ``promos`` are
    extra lines that rotate in only during the event -- the coffee's two are
    the ones the camp asked for by name.
    """

    def __init__(self, start, end, offering, title=None, promos=()):
        self.start = start
        self.end = end
        self.offering = offering
        self.title = title or offering
        self.promos = tuple(promos)

    @property
    def all_day(self) -> bool:
        return self.start is None

    def active_at(self, minutes: int) -> bool:
        """Is this event happening at ``minutes`` past midnight?"""
        if self.all_day:
            return True
        start = _minutes(self.start)
        end = _minutes(self.end) if self.end else start + 120
        return start <= minutes < end


# Coffee is the headline, so its two promos ride along whenever it is being
# poured -- first service, regular service or the last cup on burn day.
_COFFEE_PROMOS = (
    "NOW SERVING VIETNAMESE ICED COFFEE!",
    "GET YOUR GAY ICED COFFEE HERE!",
)


def _coffee(start, end, offering):
    return Event(start, end, offering, "COFFEE + TEA SERVICE", _COFFEE_PROMOS)


# The nail spa runs the whole event; one object, reused every day.
_NAIL = Event(None, None, "NAIL SPA 24/7", "24/7 DIY NAIL SPA")

# End times are assumptions -- the sheet gives only start times. They set when
# an event stops shouting "NOW", nothing more. Adjust freely.
EVENTS = {
    # Sunday: gates open. First coffee is late, at 5:30pm.
    "2026-08-30": [_coffee("17:30", "21:00", "530P FIRST COFFEE"), _NAIL],
    "2026-08-31": [_coffee("14:00", "18:00", "2P COFFEE"), _NAIL],
    "2026-09-01": [
        Event("08:30", "11:30", "830A BLOODY MARYS", "BLOODY MARY MORNINGS"),
        Event("13:00", "15:00", "1P BEARD SPA", "BEARD SPA"),
        _coffee("14:00", "18:00", "2P COFFEE"),
        Event("14:00", "17:00", "2P TAROT", "TAROT READING"),
        _NAIL,
    ],
    "2026-09-02": [
        _coffee("14:00", "18:00", "2P COFFEE"),
        Event("20:00", "23:00", "8P KARAOKE", "KARAOKE"),
        _NAIL,
    ],
    "2026-09-03": [
        Event("08:30", "11:30", "830A BLOODY MARYS", "BLOODY MARY MORNINGS"),
        Event("13:00", "15:00", "1P BEARD SPA", "BEARD SPA"),
        _coffee("14:00", "18:00", "2P COFFEE"),
        Event("14:00", "17:00", "2P TAROT", "TAROT READING"),
        _NAIL,
    ],
    "2026-09-04": [_coffee("14:00", "18:00", "2P COFFEE"), _NAIL],
    # Saturday: the Man burns tonight. Coffee is the last of the week, at noon.
    "2026-09-05": [
        _coffee("12:00", "15:00", "12P LAST COFFEE"),
        _NAIL,
        Event(None, None, "MAN BURNS TONIGHT", "THE MAN BURNS TONIGHT"),
    ],
    # Sunday: the Temple burns. Winding down -- just the spa, still open.
    "2026-09-06": [
        _NAIL,
        Event(None, None, "TEMPLE BURN TONIGHT", "THE TEMPLE BURNS TONIGHT"),
    ],
}


def _minutes(hhmm: str) -> int:
    hour, _, minute = hhmm.partition(":")
    return int(hour) * 60 + int(minute)


def events_for(date: dt.date):
    return EVENTS.get(date.isoformat(), [])


def _day_summary(date: dt.date, label: str) -> str | None:
    """"<LABEL> <offering> / <offering> / ..." or None if nothing is on.

    Ordered as the day runs -- timed events by start time, the all-day spa
    last -- so the line reads like a plan for the day. The runner pages it if
    it overruns the panel; keeping the offerings short keeps the pages few.
    """
    events = events_for(date)
    if not events:
        return None
    timed = sorted((e for e in events if not e.all_day),
                   key=lambda e: _minutes(e.start))
    all_day = [e for e in events if e.all_day]
    parts = [e.offering for e in timed + all_day]
    return "%s %s" % (label, " / ".join(parts))


def today_line(date: dt.date) -> str | None:
    return _day_summary(date, "TODAY")


def tomorrow_line(date: dt.date) -> str | None:
    return _day_summary(date + dt.timedelta(days=1), "TOMORROW")


def active_events(now: dt.datetime):
    minutes = now.hour * 60 + now.minute
    return [e for e in events_for(now.date()) if e.active_at(minutes)]


def temperature_line(reading) -> str | None:
    """"NOW 72F / 22C", or None when there is no fresh reading.

    No degree sign: the panel's font is ASCII only, so a degree glyph comes
    out a hollow box. Fahrenheit first -- this is an American desert -- with
    Celsius alongside because half the playa reads in it.
    """
    if reading is None:
        return None
    return "NOW %.0fF / %.0fC" % (reading.fahrenheit, reading.celsius)


def _message(slot_id, text, color="#ff2f6e", dwell=14.0, color_mode=0):
    """One rotation slot, as the message dict the runner already speaks.

    The id is stable per slot so the runner's advance-by-id survives the list
    being rebuilt every tick. normalize_message fills in every other field and
    clamps the ones a bad value could break.
    """
    return matrix_module.normalize_message(
        {"id": slot_id, "text": text, "color": color, "dwell": dwell,
         "color_mode": color_mode},
        default_dwell=dwell)


class Schedule:
    """Turns the clock, the calendar and the sensor into panel messages.

    ``clock`` is anything with ``now()`` and ``clock_ok()`` -- the TimeKeeper.
    ``temperature`` is a zero-argument callable returning a fresh Reading or
    None -- the sampler's ``current``. Both are read on every ``messages()``
    call, so the list is always current without this holding any state.
    """

    def __init__(self, clock, temperature=None):
        self.clock = clock
        self._temperature = temperature

    def _temp_reading(self):
        if self._temperature is None:
            return None
        try:
            return self._temperature()
        except Exception as exc:                 # a thermometer must never
            log.debug("temperature source failed: %s", exc)   # take the panel down
            return None

    def attract_now(self) -> bool:
        """Is a coffee service on right now? Drives the lights' attract look.

        Same source as the panel's coffee promos, so the sign pulls people in
        with light at the exact moment it is shouting about iced coffee. Needs
        a set clock; without one there is no date to place an event on, so it
        is simply off.
        """
        try:
            if not self.clock.clock_ok():
                return False
            now = self.clock.now()
        except Exception:
            return False
        return any(e.title == "COFFEE + TEA SERVICE" for e in active_events(now))

    def messages(self) -> list:
        """The slots to rotate, in order, right now.

        VICE always leads. Then, when the clock is set, today and tomorrow.
        Then the temperature if there is a reading. Then, only while something
        is happening, its shouts -- so a quiet afternoon is calm and a live
        event is loud.
        """
        out = [_message("sched-vice", "VICE", dwell=10.0)]

        clock_ok = False
        try:
            clock_ok = self.clock.clock_ok()
        except Exception:
            clock_ok = False

        if clock_ok:
            now = self.clock.now()
            today = today_line(now.date())
            if today:
                out.append(_message("sched-today", today, color="#22d3ee",
                                    dwell=18.0))
            tomorrow = tomorrow_line(now.date())
            if tomorrow:
                out.append(_message("sched-tomorrow", tomorrow, color="#8b5cf6",
                                    dwell=16.0))
        else:
            # No date to key on, but the spa runs the whole week regardless, so
            # it is safe to say without knowing which day it is.
            out.append(_message("sched-allday", "24/7 DIY NAIL SPA",
                                color="#22d3ee", dwell=14.0))

        temp = temperature_line(self._temp_reading())
        if temp:
            out.append(_message("sched-temp", temp, color="#2fe3b0", dwell=10.0))

        if clock_ok:
            out.extend(self._event_messages(self.clock.now()))
        return out

    def _event_messages(self, now: dt.datetime) -> list:
        """The shouts for whatever is happening at ``now``.

        Each active event contributes its promos; the coffee's two ride here.
        A single "NOW" line names everything on at once, so a passer-by sees
        the whole offer even if they catch only one slot. Nothing repeats: the
        all-day spa is already in the today line, so it earns a shout only when
        it is the *only* thing on.
        """
        active = active_events(now)
        live = [e for e in active if not e.all_day]
        out = []
        seen = set()
        for event in active:
            for promo in event.promos:
                if promo not in seen:
                    seen.add(promo)
                    out.append(_message("sched-promo-%d" % len(out), promo,
                                        color="#ff2f6e", dwell=12.0))
        titles = [e.title for e in (live or active)]
        if titles:
            out.append(_message("sched-now", "NOW: " + " / ".join(titles),
                                color="#e58b4d", dwell=14.0))
        return out
