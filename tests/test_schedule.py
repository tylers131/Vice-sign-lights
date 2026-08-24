"""Tests for the calendar-driven panel messages.

No hardware, no clock, no BLE: a fake clock and a fake temperature source
stand in, so the whole week can be walked in milliseconds.

    python3 -m unittest discover -s tests -v
"""

import datetime as dt
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights import schedule as S            # noqa: E402
from vicelights.thermometer import Reading      # noqa: E402


class FakeClock:
    def __init__(self, when, ok=True):
        self.when = when
        self.ok = ok

    def now(self):
        return self.when

    def clock_ok(self):
        return self.ok


def at(y, mo, d, h=12, mi=0, temp=None, ok=True):
    clock = FakeClock(dt.datetime(y, mo, d, h, mi), ok=ok)
    source = (lambda: Reading(temp)) if temp is not None else None
    return S.Schedule(clock, temperature=source).messages()


def texts(messages):
    return [m["text"] for m in messages]


def by_id(messages, slot_id):
    for m in messages:
        if m["id"] == slot_id:
            return m
    return None


# Tuesday of the burn week: the busy day -- bloody marys, beard spa, coffee,
# tarot, and the all-day spa.
TUE = (2026, 9, 1)


class Always(unittest.TestCase):
    def test_vice_always_leads(self):
        for day in ((2026, 8, 30), TUE, (2026, 9, 6)):
            messages = at(*day)
            self.assertEqual(messages[0]["id"], "sched-vice")
            self.assertEqual(messages[0]["text"], "VICE")

    def test_vice_shows_even_with_no_clock_and_no_sensor(self):
        messages = at(1970, 1, 1, ok=False)
        self.assertEqual(texts(messages)[0], "VICE")

    def test_every_slot_is_a_valid_message(self):
        for m in at(*TUE, temp=24.0):
            self.assertTrue(m["text"])
            self.assertIn("id", m)
            self.assertIn("color", m)
            self.assertGreater(m["dwell"], 0)


class NoBar(unittest.TestCase):
    def test_no_message_ever_mentions_a_bar(self):
        # The camp has no bar; walk every hour of every day and assert it.
        for date in S.EVENTS:
            d = dt.date.fromisoformat(date)
            for hour in range(24):
                for m in at(d.year, d.month, d.day, hour, temp=20.0):
                    self.assertNotIn("bar", m["text"].lower(), m["text"])


class TodayTomorrow(unittest.TestCase):
    def test_today_lists_the_days_offerings(self):
        today = by_id(at(*TUE), "sched-today")
        self.assertIsNotNone(today)
        for wanted in ("BLOODY", "BEARD", "COFFEE", "TAROT", "NAIL SPA"):
            self.assertIn(wanted, today["text"])
        self.assertTrue(today["text"].startswith("TODAY"))

    def test_tomorrow_is_the_next_day(self):
        # Tuesday's tomorrow is Wednesday: coffee + karaoke + spa.
        tomorrow = by_id(at(*TUE), "sched-tomorrow")
        self.assertIsNotNone(tomorrow)
        self.assertTrue(tomorrow["text"].startswith("TOMORROW"))
        self.assertIn("KARAOKE", tomorrow["text"])

    def test_timed_events_are_ordered_by_start_the_spa_last(self):
        text = by_id(at(*TUE), "sched-today")["text"]
        self.assertLess(text.index("BLOODY"), text.index("BEARD"))
        self.assertLess(text.index("BEARD"), text.index("COFFEE"))
        # The all-day spa comes after everything with a clock time.
        self.assertLess(text.index("TAROT"), text.index("NAIL SPA"))

    def test_last_day_has_no_tomorrow(self):
        # Sept 6 is the last day in the calendar; nothing follows it.
        self.assertIsNone(by_id(at(2026, 9, 6), "sched-tomorrow"))

    def test_no_today_or_tomorrow_without_a_set_clock(self):
        messages = at(1970, 1, 1, ok=False)
        self.assertIsNone(by_id(messages, "sched-today"))
        self.assertIsNone(by_id(messages, "sched-tomorrow"))

    def test_a_clockless_sign_still_names_the_all_day_spa(self):
        # It runs the whole week regardless of the date, so it is safe to show.
        self.assertIsNotNone(by_id(at(1970, 1, 1, ok=False), "sched-allday"))


class Temperature(unittest.TestCase):
    def test_temp_line_shows_both_scales(self):
        temp = by_id(at(*TUE, temp=24.0), "sched-temp")
        self.assertIsNotNone(temp)
        self.assertIn("75F", temp["text"])      # 24C rounds to 75F
        self.assertIn("24C", temp["text"])

    def test_no_temp_line_without_a_reading(self):
        self.assertIsNone(by_id(at(*TUE), "sched-temp"))

    def test_a_stale_reading_is_dropped_not_shown(self):
        # A source that returns an old reading must not put a stale number up.
        import time
        old = Reading(24.0, at=time.monotonic() - 10 * 3600)
        sch = S.Schedule(FakeClock(dt.datetime(*TUE, 12, 0)),
                         temperature=lambda: (None if old.stale() else old))
        self.assertIsNone(by_id(sch.messages(), "sched-temp"))

    def test_no_degree_glyph_the_panel_font_lacks_it(self):
        temp = by_id(at(*TUE, temp=24.0), "sched-temp")
        self.assertNotIn("°", temp["text"])

    def test_a_failing_temp_source_never_crashes_the_build(self):
        def boom():
            raise RuntimeError("sensor on fire")
        sch = S.Schedule(FakeClock(dt.datetime(*TUE, 12, 0)), temperature=boom)
        messages = sch.messages()            # must not raise
        self.assertEqual(messages[0]["text"], "VICE")
        self.assertIsNone(by_id(messages, "sched-temp"))


class DuringEvents(unittest.TestCase):
    def test_coffee_promos_appear_while_coffee_is_served(self):
        # 2:30pm Tuesday: coffee runs 2-6pm.
        promos = texts(at(2026, 9, 1, 14, 30))
        self.assertIn("NOW SERVING VIETNAMESE ICED COFFEE!", promos)
        self.assertIn("GET YOUR GAY ICED COFFEE HERE!", promos)

    def test_coffee_promos_gone_once_coffee_ends(self):
        # 7pm Tuesday: coffee is over, tarot is over, spa still on.
        promos = texts(at(2026, 9, 1, 19, 0))
        self.assertNotIn("NOW SERVING VIETNAMESE ICED COFFEE!", promos)

    def test_the_now_line_names_what_is_on(self):
        now = by_id(at(2026, 9, 1, 14, 30), "sched-now")
        self.assertIsNotNone(now)
        self.assertIn("COFFEE", now["text"])
        self.assertIn("TAROT", now["text"])
        self.assertTrue(now["text"].startswith("NOW:"))

    def test_promos_do_not_duplicate_when_two_coffees_overlap(self):
        # Only one coffee runs at a time, but the de-dupe must hold regardless.
        promos = [t for t in texts(at(2026, 9, 1, 14, 30))
                  if "ICED COFFEE" in t]
        self.assertEqual(len(promos), len(set(promos)))

    def test_a_quiet_hour_has_no_promos_but_still_has_the_basics(self):
        # 4am Tuesday: only the all-day spa is on.
        messages = at(2026, 9, 1, 4, 0)
        self.assertNotIn("GET YOUR GAY ICED COFFEE HERE!", texts(messages))
        self.assertEqual(messages[0]["text"], "VICE")
        self.assertIsNotNone(by_id(messages, "sched-today"))

    def test_karaoke_night(self):
        now = by_id(at(2026, 9, 2, 20, 30), "sched-now")
        self.assertIn("KARAOKE", now["text"])


class StableIds(unittest.TestCase):
    def test_ids_are_stable_across_rebuilds_within_a_slot(self):
        # The runner advances by id, so the same moment must give the same ids
        # every time it is asked -- otherwise rotation would never advance.
        a = [m["id"] for m in at(*TUE, temp=24.0)]
        b = [m["id"] for m in at(*TUE, temp=24.0)]
        self.assertEqual(a, b)

    def test_ids_are_unique_within_one_build(self):
        ids = [m["id"] for m in at(2026, 9, 1, 14, 30, temp=24.0)]
        self.assertEqual(len(ids), len(set(ids)))


class ActiveHelper(unittest.TestCase):
    def test_all_day_events_are_always_active(self):
        for hour in (0, 6, 12, 23):
            active = S.active_events(dt.datetime(2026, 9, 1, hour, 0))
            self.assertTrue(any(e.all_day for e in active))

    def test_a_timed_event_is_inactive_before_and_after(self):
        # Beard spa 1-3pm.
        self.assertFalse(any(e.offering.endswith("BEARD SPA")
                             for e in S.active_events(dt.datetime(2026, 9, 1, 12, 0))))
        self.assertTrue(any("BEARD" in e.title
                            for e in S.active_events(dt.datetime(2026, 9, 1, 13, 30))))
        self.assertFalse(any("BEARD" in e.title
                             for e in S.active_events(dt.datetime(2026, 9, 1, 15, 30))))


if __name__ == "__main__":
    unittest.main(verbosity=2)
