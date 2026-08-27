"""Tests for driving more than one text panel from the same text.

Both signs are identical hardware showing the same message, so the config folds
them into one canonical ``panels`` list and a panel write becomes one job with
one item per enabled panel -- the same frames sent to each.

    python3 -m unittest discover -s tests -v
"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights.config import ConfigStore              # noqa: E402
from vicelights.ble import BleWorker                    # noqa: E402

CHAR = "0000fff3-0000-1000-8000-00805f9b34fb"
A = "AA:BB:CC:DD:EE:01"
B = "AA:BB:CC:DD:EE:02"


def _store(matrix):
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump({"matrix": matrix}, handle)
    handle.close()
    store = ConfigStore(handle.name)
    for path in (handle.name, handle.name + ".lastgood", handle.name + ".bak"):
        if os.path.exists(path):
            os.unlink(path)
    return store


class PanelsNormalisation(unittest.TestCase):
    def test_legacy_single_address_becomes_panel_one(self):
        store = _store({"enabled": True, "address": A, "name": "Sign 1"})
        panels = store.matrix()["panels"]
        self.assertEqual([(p["address"], p["name"]) for p in panels], [(A, "Sign 1")])

    def test_primary_mirrors_the_first_panel(self):
        store = _store({"panels": [{"address": A, "name": "One"},
                                   {"address": B, "name": "Two"}]})
        matrix = store.matrix()
        self.assertEqual(matrix["address"], A)      # legacy field points at #1
        self.assertEqual(matrix["name"], "One")
        self.assertEqual(len(matrix["panels"]), 2)

    def test_legacy_address_folded_in_without_duplicating(self):
        store = _store({"address": A, "name": "Sign 1",
                        "panels": [{"address": A, "name": "Sign 1"},
                                   {"address": B, "name": "Sign 2"}]})
        addrs = [p["address"] for p in store.matrix()["panels"]]
        self.assertEqual(addrs, [A, B])             # A not listed twice

    def test_duplicate_and_bad_addresses_dropped(self):
        store = _store({"panels": [{"address": A}, {"address": A},
                                   {"address": "garbage"}]})
        self.assertEqual([p["address"] for p in store.matrix()["panels"]], [A])

    def test_per_panel_enabled_defaults_true_and_persists_false(self):
        store = _store({"panels": [{"address": A},
                                   {"address": B, "enabled": False}]})
        panels = {p["address"]: p["enabled"] for p in store.matrix()["panels"]}
        self.assertTrue(panels[A])
        self.assertFalse(panels[B])

    def test_empty_matrix_has_no_panels(self):
        store = _store({})
        self.assertEqual(store.matrix()["panels"], [])


class SubmitMirrors(unittest.TestCase):
    def _worker(self, store):
        worker = BleWorker(store)
        worker._register = lambda job: job          # skip the started-thread guard
        return worker

    def test_two_panels_two_items_same_frames(self):
        store = _store({"enabled": True, "char_uuid": CHAR,
                        "panels": [{"address": A, "name": "Sign 1"},
                                   {"address": B, "name": "Sign 2"}]})
        job = self._worker(store).submit_matrix([b"\x01\x02", b"\x03"], "hello")
        self.assertEqual([i["address"] for i in job.items], [A, B])
        self.assertEqual(job.items[0]["frames"], job.items[1]["frames"])
        # both carry a write characteristic
        self.assertTrue(all(i.get("char_uuid") for i in job.items))

    def test_disabled_panel_is_skipped(self):
        store = _store({"enabled": True, "char_uuid": CHAR,
                        "panels": [{"address": A}, {"address": B, "enabled": False}]})
        job = self._worker(store).submit_matrix([b"\x01"], "hi")
        self.assertEqual([i["address"] for i in job.items], [A])

    def test_no_panels_drops_the_write(self):
        store = _store({"enabled": True})
        self.assertIsNone(self._worker(store).submit_matrix([b"\x01"], "hi"))

    def test_legacy_only_still_sends(self):
        # A config that never grew a panels list still writes to its address.
        store = _store({"enabled": True, "address": A, "char_uuid": CHAR})
        job = self._worker(store).submit_matrix([b"\x01"], "hi")
        self.assertEqual([i["address"] for i in job.items], [A])


class _Clock:
    def __init__(self, when):
        self.when = when

    def clock_ok(self):
        return self.when is not None

    def now(self):
        return self.when


class _Sched:
    def __init__(self, when):
        self.clock = _Clock(when)


class _SendWorker:
    """Records panel control sends; each submit reports a completed job."""

    busy = False

    def __init__(self):
        self.sent = []

    def submit_matrix(self, frames, label, **kwargs):
        self.sent.append(label)

        class Job:
            state, ok = "done", 1
        return Job()

    def resting_sends(self):
        return [s for s in self.sent if "resting" in s]


class NightDim(unittest.TestCase):
    """The panel dims on a clock window; the LED strips are never touched here."""

    def setUp(self):
        self.store = _store({"enabled": True, "address": A, "family": "ipixel",
                             "char_uuid": CHAR, "brightness": 100,
                             "night_dim_enabled": True, "night_dim_start": "23:00",
                             "night_dim_end": "06:00", "night_brightness": 15})

    def _runner(self, when):
        from vicelights.messages import MatrixRunner
        return MatrixRunner(self.store, _SendWorker(), schedule=_Sched(when))

    def _target(self, hour, minute=0, clock=True):
        import datetime as dt
        when = dt.datetime(2026, 9, 2, hour, minute) if clock else None
        return self._runner(when)._target_brightness(self.store.matrix())

    def test_dim_inside_window_bright_outside(self):
        for hour in (23, 0, 3, 5):
            self.assertEqual(self._target(hour), 15, "%02d:00 should dim" % hour)
        for hour in (12, 22, 6, 7):
            self.assertEqual(self._target(hour), 100, "%02d:00 should be full" % hour)

    def test_no_clock_stays_bright(self):
        self.assertEqual(self._target(1, clock=False), 100)

    def test_disabled_stays_at_day_level(self):
        self.store.update_matrix({"night_dim_enabled": False})
        self.assertEqual(self._target(1), 100)

    def test_command_sent_once_per_boundary(self):
        import datetime as dt
        runner = self._runner(dt.datetime(2026, 9, 2, 1, 0))    # night
        runner._apply_night_dim(self.store.matrix())
        runner._apply_night_dim(self.store.matrix())            # no change
        self.assertEqual(len(runner.worker.sent), 1)
        self.assertEqual(runner._applied_brightness, 15)
        runner.schedule = _Sched(dt.datetime(2026, 9, 2, 12, 0))  # cross to day
        runner._apply_night_dim(self.store.matrix())
        self.assertEqual(len(runner.worker.sent), 2)
        self.assertEqual(runner._applied_brightness, 100)

    def test_strips_are_not_affected(self):
        # force_full_brightness governs the strips and is independent of this.
        self.assertTrue(self.store.setting("force_full_brightness"))


class Scrolling(unittest.TestCase):
    """The panel's native scroll: on for long messages, off is paging."""

    def _driver(self, text_mode, animation):
        from vicelights import matrix as M
        return M.driver_for({"family": "ipixel", "char_uuid": CHAR,
                             "text_mode": text_mode, "text_animation": animation})

    def test_scroll_default_applies_to_a_message_without_a_mode(self):
        from vicelights import matrix as M
        d = self._driver("native", "scroll")
        self.assertTrue(d.animates)
        self.assertEqual(d.animation_for({"text": "hi"}), M.TEXT_ANIMATIONS["scroll"])
        self.assertEqual(d.mode_for({"text": "hi"}), "scroll")

    def test_a_message_keeps_its_own_mode(self):
        from vicelights import matrix as M
        d = self._driver("native", "scroll")
        self.assertEqual(d.animation_for({"text": "hi", "mode": "static"}),
                         M.TEXT_ANIMATIONS["static"])

    def test_a_normalized_schedule_line_inherits_the_scroll_default(self):
        # A schedule/preview line never names a mode, so normalize_message must
        # leave it unset -- otherwise it looks like a deliberate "static" hold
        # and the panel's scroll default never reaches the calendar text.
        from vicelights import matrix as M
        message = M.normalize_message({"text": "TODAY 2P COFFEE"})
        self.assertEqual(message["mode"], "")
        d = self._driver("native", "scroll")
        self.assertEqual(d.mode_for(message), "scroll")
        self.assertEqual(d.animation_for(message), M.TEXT_ANIMATIONS["scroll"])

    def test_a_bad_mode_still_falls_back_to_static(self):
        from vicelights import matrix as M
        self.assertEqual(M.normalize_message({"text": "hi", "mode": "wat"})["mode"],
                         "static")

    def test_pixels_mode_never_scrolls(self):
        d = self._driver("pixels", "scroll")
        self.assertFalse(d.animates)         # pages in software, does not scroll
        self.assertEqual(d.modes, ("static",))

    def test_config_rejects_a_bad_animation(self):
        store = _store({"text_animation": "wobble"})
        self.assertEqual(store.matrix()["text_animation"], "static")


class ScrollSpeed(unittest.TestCase):
    """One slider slows every scroll; a message's own speed still wins."""

    def _driver(self, **cfg):
        from vicelights import matrix as M
        base = {"family": "ipixel", "char_uuid": CHAR, "text_mode": "native",
                "text_animation": "scroll"}
        base.update(cfg)
        return M.driver_for(base)

    def test_unspecified_speed_is_left_unset(self):
        from vicelights import matrix as M
        self.assertIsNone(M.normalize_message({"text": "hi"})["speed"])

    def test_a_line_without_a_speed_inherits_the_panel_default(self):
        from vicelights import matrix as M
        d = self._driver(text_speed=20)
        self.assertEqual(d.speed_for(M.normalize_message({"text": "SLOW"})), 20)

    def test_a_message_keeps_its_own_speed(self):
        from vicelights import matrix as M
        d = self._driver(text_speed=20)
        self.assertEqual(d.speed_for(M.normalize_message({"text": "hi", "speed": 70})),
                         70)

    def test_default_text_speed_is_calmer_than_the_old_fifty(self):
        store = _store({"text_speed": None})
        from vicelights import matrix as M
        self.assertEqual(store.matrix()["text_speed"], M.DEFAULT_TEXT_SPEED)
        self.assertLess(M.DEFAULT_TEXT_SPEED, 50)

    def test_config_clamps_a_wild_speed(self):
        self.assertEqual(_store({"text_speed": 999}).matrix()["text_speed"], 100)
        self.assertEqual(_store({"text_speed": -5}).matrix()["text_speed"], 0)

    def test_only_travelling_animations_count_as_scrolling(self):
        self.assertTrue(self._driver(text_animation="scroll").scrolls({"text": "x"}))
        self.assertTrue(self._driver(text_animation="marquee").scrolls({"text": "x"}))
        self.assertFalse(self._driver(text_animation="flash").scrolls({"text": "x"}))
        self.assertFalse(self._driver(text_animation="static").scrolls({"text": "x"}))


class RestingLine(unittest.TestCase):
    """When nothing plays, every sign rests on VICE rather than stale text."""

    def _runner(self, **matrix):
        base = {"enabled": True, "address": A, "family": "ipixel",
                "char_uuid": CHAR, "text_mode": "native",
                "text_animation": "scroll"}
        base.update(matrix)
        store = _store(base)
        from vicelights.messages import MatrixRunner
        return MatrixRunner(store, _SendWorker()), store

    def test_idle_tick_rests_on_the_configured_text(self):
        runner, _ = self._runner(resting_text="VICE")
        runner.tick()
        self.assertTrue(runner._resting)
        self.assertEqual((runner._current or {}).get("text"), "VICE")
        self.assertEqual(len(runner.worker.resting_sends()), 1)

    def test_rest_is_painted_once_not_every_tick(self):
        runner, _ = self._runner()
        runner.tick()
        runner.tick()
        runner.tick()
        self.assertEqual(len(runner.worker.resting_sends()), 1)

    def test_disabling_resting_leaves_the_panel_alone(self):
        runner, _ = self._runner(resting_enabled=False)
        runner.tick()
        self.assertFalse(runner._resting)
        self.assertEqual(runner.worker.resting_sends(), [])

    def test_a_blank_is_not_refilled_by_the_resting_line(self):
        runner, _ = self._runner()
        runner.clear()
        runner.tick()
        self.assertEqual(runner.worker.resting_sends(), [])   # blank stays blank
        self.assertTrue(runner._rest_suppressed)

    def test_hold_extends_a_long_scroll_past_its_dwell(self):
        from vicelights import matrix as M
        runner, store = self._runner(scroll_min_seconds=6.0,
                                     scroll_seconds_per_char=0.5)
        matrix = store.matrix()
        driver = M.driver_for(matrix)
        short = M.normalize_message({"text": "HI", "dwell": 10.0})
        long = M.normalize_message({"text": "X" * 40, "dwell": 10.0})
        self.assertEqual(runner._hold_for(matrix, driver, short, 10.0), 10.0)
        self.assertEqual(runner._hold_for(matrix, driver, long, 10.0), 6.0 + 40 * 0.5)

    def test_a_held_message_is_not_stretched(self):
        from vicelights import matrix as M
        runner, store = self._runner(text_animation="static")
        matrix = store.matrix()
        driver = M.driver_for(matrix)
        message = M.normalize_message({"text": "X" * 40, "dwell": 8.0})
        self.assertEqual(runner._hold_for(matrix, driver, message, 8.0), 8.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
