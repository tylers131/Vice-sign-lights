"""Tests for the around-the-clock light show and its time-of-day rotation.

No hardware, no radio: a temp-file config with the real twelve-zone layout and
a fake clock stand in, so the whole day can be walked in milliseconds.

    python3 -m unittest discover -s tests -v

What these lock down:

* every rotating scene lights all twelve zones -- the whole point was to stop
  finding zones dark;
* every scene uses a real animation mode and a real colour;
* every name a day-part, the attract list or the fallback references is a scene
  that exists (a typo here is a scene that silently never plays);
* the day-parts tile the whole 24 hours with no gap, and resolve correctly by
  the clock, including the wrap past midnight;
* a coffee service overrides the hour with the attract look, even with no clock;
* with the clock unset the sign still has a good look (the fallback);
* and the scheduler actually switches mood at a day-part boundary.
"""

import datetime as dt
import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights import lightshow as L                 # noqa: E402
from vicelights.config import ConfigStore             # noqa: E402
from vicelights.protocol import MODE_MIN, MODE_MAX     # noqa: E402
from vicelights.scheduler import Rotation             # noqa: E402

HEX = re.compile(r"^#[0-9a-fA-F]{6}$")

# The real sign's twelve controllers and their group memberships: two per
# letter (all also in "letters"), two cups, two straws (all also in "drink").
# The scenes address group:letters / group:cup / group:straw / group:V..E, so a
# scene that covers those covers the fleet.
FLEET = [
    ("A_V", ["letters", "V", "side-a"]), ("B_V", ["letters", "V", "side-b"]),
    ("A_I", ["letters", "I", "side-a"]), ("B_I", ["letters", "I", "side-b"]),
    ("A_C", ["letters", "C", "side-a"]), ("B_C", ["letters", "C", "side-b"]),
    ("A_E", ["letters", "E", "side-a"]), ("B_E", ["letters", "E", "side-b"]),
    ("A_Cup", ["cup", "drink", "side-a"]), ("B_Cup", ["cup", "drink", "side-b"]),
    ("A_Straw", ["straw", "drink", "side-a"]),
    ("B_Straw", ["straw", "drink", "side-b"]),
]


def _address(index: int) -> str:
    return "BE:00:00:00:00:%02X" % index


def build_store() -> ConfigStore:
    """A store with the twelve-zone fleet and the light show applied."""
    devices = [{"address": _address(i), "name": name, "groups": groups,
                "enabled": True}
               for i, (name, groups) in enumerate(FLEET)]
    raw = {"devices": devices, "scenes": [], "schedules": [],
           "rotation": {"enabled": False}}
    handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(raw, handle)
    handle.close()
    store = ConfigStore(handle.name)
    store.replace_all(raw)
    L.apply(store)
    # Clean the temp files up once the store has read them.
    for path in (handle.name, handle.name + ".lastgood", handle.name + ".bak"):
        if os.path.exists(path):
            os.unlink(path)
    return store


class FakeClock:
    def __init__(self, when=None):
        self.when = when

    def clock_ok(self):
        return self.when is not None

    def now(self):
        return self.when


class FakeWorker:
    """Enough of the BLE worker for rotation: never busy, records submissions."""

    def __init__(self):
        self.busy = False
        self.last_manual_at = None
        self.submitted = []

    def submit_scene(self, scene):
        self.submitted.append(scene["name"])


class SceneCoverage(unittest.TestCase):
    def setUp(self):
        self.store = build_store()
        self.all_addrs = {d["address"] for d in self.store.devices(enabled_only=True)}

    def test_twelve_zones(self):
        self.assertEqual(len(self.all_addrs), 12)

    def test_every_rotating_scene_lights_every_zone(self):
        for scene in self.store.scenes():
            if scene["name"] == "All off":
                continue
            covered = set()
            for step in scene["steps"]:
                covered |= set(self.store.resolve_target(step["target"]))
            self.assertEqual(covered, self.all_addrs,
                             "scene %r leaves zones dark: %s" %
                             (scene["name"], sorted(self.all_addrs - covered)))

    def test_all_off_is_the_only_powered_off_scene(self):
        off = [s["name"] for s in self.store.scenes()
               if any(step.get("power") is False for step in s["steps"])]
        self.assertEqual(off, ["All off"])


class SceneValidity(unittest.TestCase):
    def test_modes_and_colours_are_real(self):
        for scene in L.SCENES:
            for step in scene["steps"]:
                mode = step.get("mode")
                if mode is not None:
                    self.assertTrue(MODE_MIN <= mode <= MODE_MAX,
                                    "scene %r mode 0x%02x out of range" %
                                    (scene["name"], mode))
                color = step.get("color")
                if color is not None and "color" in step:
                    self.assertRegex(color, HEX,
                                     "scene %r has a bad colour %r" %
                                     (scene["name"], color))

    def test_scene_names_are_unique(self):
        names = [s["name"] for s in L.SCENES]
        self.assertEqual(len(names), len(set(names)))


class PlaylistIntegrity(unittest.TestCase):
    """Every name referenced by the schedule must be a scene that exists."""

    def setUp(self):
        self.names = {s["name"] for s in L.SCENES}

    def test_daypart_playlists_reference_real_scenes(self):
        for part in L.DAYPARTS:
            for name in part["playlist"]:
                self.assertIn(name, self.names,
                              "day-part %r names a missing scene %r" %
                              (part["name"], name))
            self.assertTrue(part["playlist"],
                            "day-part %r has an empty playlist" % part["name"])

    def test_attract_and_fallback_reference_real_scenes(self):
        for name in L.ATTRACT:
            self.assertIn(name, self.names)
        for name in L.FALLBACK_PLAYLIST:
            self.assertIn(name, self.names)

    def test_boot_scene_exists(self):
        self.assertIn(L.BOOT_SCENE, self.names)

    def test_excluded_scene_is_never_in_a_playlist(self):
        # "All off" must not appear in any rotating list.
        lists = [L.FALLBACK_PLAYLIST, L.ATTRACT] + [p["playlist"] for p in L.DAYPARTS]
        for names in lists:
            self.assertNotIn("All off", names)


class DaypartCoverage(unittest.TestCase):
    """The day-parts must tile all 24 hours with no gap."""

    def setUp(self):
        self.dayparts = L.rotation_config()["dayparts"]

    def test_sorted_and_starts_at_midnight(self):
        starts = [p["start"] for p in self.dayparts]
        self.assertEqual(starts, sorted(starts))
        self.assertEqual(starts[0], "00:00",
                         "first day-part must start at 00:00 so every minute maps")

    def test_every_minute_resolves_to_a_daypart(self):
        seen = set()
        for minutes in range(24 * 60):
            now = dt.datetime(2026, 9, 2, minutes // 60, minutes % 60)
            part = Rotation._active_daypart(self.dayparts, now)
            self.assertIsNotNone(part)
            seen.add(part["name"])
        self.assertEqual(seen, {p["name"] for p in self.dayparts},
                         "some day-part is never reached")

    def test_boundaries_resolve_to_the_starting_part(self):
        for part in self.dayparts:
            hour, minute = (int(x) for x in part["start"].split(":"))
            now = dt.datetime(2026, 9, 2, hour, minute)
            chosen = Rotation._active_daypart(self.dayparts, now)
            self.assertEqual(chosen["name"], part["name"])

    def test_wrap_before_first_start_uses_the_last_part(self):
        # A list that does not begin at midnight: pre-first-start wraps back.
        parts = [{"name": "day", "start": "09:00", "interval_minutes": 10.0,
                  "playlist": ["Vice"]},
                 {"name": "night", "start": "20:00", "interval_minutes": 5.0,
                  "playlist": ["Miami"]}]
        chosen = Rotation._active_daypart(parts, dt.datetime(2026, 9, 2, 3, 0))
        self.assertEqual(chosen["name"], "night")


class Resolution(unittest.TestCase):
    """Rotation._resolve: attract > day-part > fallback."""

    def setUp(self):
        self.store = build_store()

    def _rotation(self, when=None, coffee=False):
        rot = Rotation(self.store, FakeWorker(), timekeeper=FakeClock(when),
                       service_active=lambda: coffee)
        return rot

    def test_daypart_by_clock(self):
        rot = self._rotation(dt.datetime(2026, 9, 2, 22, 0))   # party time
        key, playlist, interval = rot._resolve(self.store.rotation())
        self.assertEqual(key, "daypart:Party")
        self.assertEqual(interval, 3.0)
        self.assertIn("Miami", playlist)

    def test_late_night_wraps_past_midnight(self):
        rot = self._rotation(dt.datetime(2026, 9, 2, 3, 0))
        key, _, _ = rot._resolve(self.store.rotation())
        self.assertEqual(key, "daypart:Late night")

    def test_coffee_overrides_the_hour(self):
        rot = self._rotation(dt.datetime(2026, 9, 2, 14, 0), coffee=True)
        key, playlist, interval = rot._resolve(self.store.rotation())
        self.assertEqual(key, "attract")
        self.assertEqual(playlist, L.ATTRACT)
        self.assertEqual(interval, L.ATTRACT_INTERVAL_MINUTES)

    def test_attract_works_without_a_clock(self):
        rot = self._rotation(when=None, coffee=True)
        key, playlist, _ = rot._resolve(self.store.rotation())
        self.assertEqual(key, "attract")

    def test_fallback_when_clock_unset(self):
        rot = self._rotation(when=None, coffee=False)
        key, playlist, interval = rot._resolve(self.store.rotation())
        self.assertEqual(key, "base")
        self.assertEqual(interval, self.store.rotation()["interval_minutes"])
        # The fallback resolves to real scenes.
        names = self.store.rotation_scenes(playlist)
        self.assertTrue(names)
        self.assertNotIn("All off", names)

    def test_service_active_raising_is_treated_as_off(self):
        def boom():
            raise RuntimeError("calendar exploded")
        rot = Rotation(self.store, FakeWorker(),
                       timekeeper=FakeClock(dt.datetime(2026, 9, 2, 14, 0)),
                       service_active=boom)
        key, _, _ = rot._resolve(self.store.rotation())
        self.assertEqual(key, "daypart:Daytime")   # falls through, does not crash


class BoundarySwitch(unittest.TestCase):
    """A tick after the mood changes should switch promptly, not wait it out."""

    def setUp(self):
        self.store = build_store()
        self.clock = FakeClock(dt.datetime(2026, 9, 2, 12, 0))   # Daytime
        self.worker = FakeWorker()
        self.rot = Rotation(self.store, self.worker, timekeeper=self.clock,
                            service_active=lambda: False)

    def test_switches_playlist_at_a_daypart_boundary(self):
        # Establish the daytime mood as if a boot scene had been shown.
        self.rot.note_played("Vice")
        self.assertEqual(self.rot._active_key, "daypart:Daytime")

        # Jump the clock into the party window and tick.
        self.clock.when = dt.datetime(2026, 9, 2, 21, 0)
        self.rot.tick()

        self.assertEqual(self.rot._active_key, "daypart:Party")
        self.assertTrue(self.worker.submitted, "nothing was played on the switch")
        party = set(self.store.rotation_scenes(
            [p for p in L.DAYPARTS if p["name"] == "Party"][0]["playlist"]))
        self.assertIn(self.worker.submitted[-1], party)

    def test_manual_hold_defers_the_switch(self):
        import time
        self.rot.note_played("Vice")
        self.clock.when = dt.datetime(2026, 9, 2, 21, 0)
        # Simulate a manual command just now: hold is 20 min in the show config.
        self.worker.last_manual_at = time.monotonic()
        self.rot.tick()
        # Held: nothing played, and the mood key has not advanced yet.
        self.assertEqual(self.worker.submitted, [])
        self.assertEqual(self.rot._active_key, "daypart:Daytime")


class SelfHeal(unittest.TestCase):
    """A mood must keep re-sending -- that is the fix for dropped controllers.

    Regressions for the review's confirmed self-heal defects: a single-scene
    mood was skipped entirely, and a mood whose playlist matched nothing froze.
    """

    def setUp(self):
        self.store = build_store()
        self.worker = FakeWorker()

    def _rot(self, when=None, coffee=False):
        return Rotation(self.store, self.worker, timekeeper=FakeClock(when),
                        service_active=lambda: coffee)

    def test_single_scene_mood_is_resent_every_interval(self):
        # A one-scene fallback (clock unset) must still be sent, and re-sent.
        self.store.update_rotation({"enabled": True, "playlist": ["Vice"],
                                    "dayparts": [], "attract": []})
        rot = self._rot(when=None)
        self.assertEqual(rot.play_next(), "Vice")
        self.assertEqual(rot.play_next(), "Vice")     # re-sent, not skipped
        self.assertEqual(self.worker.submitted, ["Vice", "Vice"])

    def test_empty_resolved_playlist_falls_back_not_freezes(self):
        # A day-part naming a scene that does not exist must not freeze rotation.
        self.store.update_rotation({
            "enabled": True, "playlist": ["Vice", "Miami"],
            "dayparts": [{"name": "Void", "start": "00:00",
                          "interval_minutes": 5.0, "playlist": ["No Such Scene"]}],
            "attract": []})
        rot = self._rot(when=dt.datetime(2026, 9, 2, 0, 30))
        key, names, _ = rot._resolve_scenes(self.store.rotation())
        self.assertEqual(key, "daypart:Void")
        self.assertTrue(names, "empty day-part should fall back, not freeze")
        played = rot.play_next()
        self.assertIn(played, ("Vice", "Miami"))
        self.assertTrue(self.worker.submitted)

    def test_manual_next_at_boundary_draws_from_the_new_mood(self):
        # A forced next landing on a boundary must not play the old bag.
        rot = self._rot(when=dt.datetime(2026, 9, 2, 19, 0))   # Golden hour
        rot.note_played("Sunset")
        self.assertEqual(rot._active_key, "daypart:Golden hour")
        rot.timekeeper.when = dt.datetime(2026, 9, 2, 21, 0)    # Party
        played = rot.play_next(force=True)
        party = set(self.store.rotation_scenes(
            [p for p in L.DAYPARTS if p["name"] == "Party"][0]["playlist"]))
        self.assertIn(played, party)
        self.assertEqual(rot._active_key, "daypart:Party")


class AutoOff(unittest.TestCase):
    """The nightly downtime: dark on a wall-clock window, waking into the show."""

    def setUp(self):
        self.store = build_store()
        self.store.update_rotation({"enabled": True, "auto_off_enabled": True,
                                    "auto_off_at": "23:00", "auto_on_at": "06:00"})

    def _rot(self, when=None, coffee=False):
        return Rotation(self.store, FakeWorker(), timekeeper=FakeClock(when),
                        service_active=lambda: coffee)

    def _key(self, hour, minute=0, when=True, coffee=False):
        clock = dt.datetime(2026, 9, 2, hour, minute) if when else None
        return self._rot(clock, coffee)._resolve_scenes(self.store.rotation())[0]

    def test_dark_inside_the_window_wrapping_midnight(self):
        for hour in (23, 0, 3, 5):
            self.assertEqual(self._key(hour), "quiet", "%02d:00 should be dark" % hour)

    def test_lit_outside_the_window(self):
        self.assertNotEqual(self._key(22), "quiet")     # before it starts
        self.assertNotEqual(self._key(6), "quiet")      # exactly at wake
        self.assertNotEqual(self._key(12), "quiet")

    def test_quiet_resolves_to_the_blackout_scene(self):
        rot = self._rot(dt.datetime(2026, 9, 2, 2, 0))
        key, names, _ = rot._resolve_scenes(self.store.rotation())
        self.assertEqual(key, "quiet")
        self.assertEqual(names, ["All off"])
        self.assertEqual(rot.play_next(), "All off")    # actually sends it

    def test_auto_off_beats_coffee_attract(self):
        # An explicit "be dark now" wins even over a coffee service.
        self.assertEqual(self._key(2, coffee=True), "quiet")

    def test_no_clock_never_auto_offs(self):
        self.assertNotEqual(self._key(2, when=False), "quiet")

    def test_zero_length_window_is_never_dark(self):
        self.store.update_rotation({"auto_off_at": "06:00", "auto_on_at": "06:00"})
        self.assertNotEqual(self._key(6), "quiet")

    def test_disabled_runs_the_normal_show(self):
        self.store.update_rotation({"auto_off_enabled": False})
        self.assertTrue(self._key(2).startswith("daypart:"))

    def test_label_and_status_expose_the_window(self):
        rot = self._rot(dt.datetime(2026, 9, 2, 2, 0))
        st = rot.status()
        self.assertTrue(st["auto_off_enabled"])
        self.assertEqual(st["auto_off_at"], "23:00")
        self.assertEqual(st["auto_on_at"], "06:00")
        self.assertTrue(st["sleeping"])
        self.assertEqual(st["daypart"], "Auto off")


class Editors(unittest.TestCase):
    """The phone's mid-week edits: day-part retuning and coffee overrides."""

    def setUp(self):
        self.store = build_store()

    def test_daypart_edit_preserves_playlists(self):
        dayparts = [dict(d) for d in self.store.rotation()["dayparts"]]
        for part in dayparts:
            if part["name"] == "Party":
                part["start"] = "21:00"
                part["interval_minutes"] = 4.0
        self.store.update_rotation({"dayparts": dayparts})
        party = [d for d in self.store.rotation()["dayparts"]
                 if d["name"] == "Party"][0]
        self.assertEqual(party["start"], "21:00")
        self.assertEqual(party["interval_minutes"], 4.0)
        self.assertEqual(len(party["playlist"]), 6)     # scenes not lost
        self.assertEqual(len(self.store.rotation()["dayparts"]), 5)

    def test_coffee_override_set_and_clear(self):
        self.store.set_coffee_override("2026-09-02",
                                       {"enabled": True, "start": "19:00", "end": "22:00"})
        got = self.store.coffee_overrides().get("2026-09-02")
        self.assertEqual((got["start"], got["end"], got["enabled"]),
                         ("19:00", "22:00", True))
        self.assertTrue(self.store.clear_coffee_override("2026-09-02"))
        self.assertNotIn("2026-09-02", self.store.coffee_overrides())

    def test_coffee_override_normalises_bad_input(self):
        # A bad time falls back to the default; a bad date key is dropped.
        self.store.set_coffee_override("2026-09-02", {"start": "nope"})
        self.assertEqual(self.store.coffee_overrides()["2026-09-02"]["start"], "14:00")
        raw = self.store.snapshot()
        raw["coffee"] = {"overrides": {"not-a-date": {"start": "10:00"}, 5: {}}}
        self.store.replace_all(raw)
        self.assertEqual(self.store.coffee_overrides(), {})


class ConfigHardening(unittest.TestCase):
    """A valid-JSON but structurally-bad config must never crash startup."""

    def _loads(self, raw):
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(raw, handle)
        handle.close()
        try:
            return ConfigStore(handle.name).snapshot()
        finally:
            for path in (handle.name, handle.name + ".lastgood",
                         handle.name + ".bak"):
                if os.path.exists(path):
                    os.unlink(path)

    def test_scalar_where_a_list_belongs_does_not_raise(self):
        for raw in ({"rotation": {"attract": True}},
                    {"rotation": {"dayparts": 8}},
                    {"rotation": "on"},
                    {"settings": "loud"},
                    {"devices": 5},
                    {"scenes": "no"},
                    {"mode_names": "x"}):
            snap = self._loads(raw)                # must not raise
            self.assertIn("rotation", snap)
            self.assertIsInstance(snap["rotation"]["attract"], list)
            self.assertIsInstance(snap["rotation"]["dayparts"], list)


class ApplyEffects(unittest.TestCase):
    def test_apply_clears_blackout_schedule_and_sets_boot(self):
        devices = [{"address": _address(i), "name": name, "groups": groups,
                    "enabled": True}
                   for i, (name, groups) in enumerate(FLEET)]
        raw = {"devices": devices,
               "schedules": [
                   {"name": "Dawn off", "scene": "All off", "time": "06:30"},
                   {"name": "Keep me", "scene": "Vice", "time": "19:30"}],
               "scenes": [], "rotation": {"enabled": False}}
        handle = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(raw, handle)
        handle.close()
        store = ConfigStore(handle.name)
        store.replace_all(raw)
        L.apply(store)

        names = [s["name"] for s in store.schedules()]
        self.assertNotIn("Dawn off", names)      # blackout gone
        self.assertIn("Keep me", names)          # user schedule kept
        self.assertEqual(store.setting("apply_on_boot"), L.BOOT_SCENE)
        self.assertTrue(store.rotation()["enabled"])

        for path in (handle.name, handle.name + ".lastgood", handle.name + ".bak"):
            if os.path.exists(path):
                os.unlink(path)

    def test_apply_preserves_devices_and_mode_names(self):
        store = build_store()
        self.assertEqual(len(store.devices()), 12)
        before = len(store.devices())
        L.apply(store)                            # second apply is idempotent
        self.assertEqual(len(store.devices()), before)
        self.assertEqual(len(store.scenes()), len(L.SCENES))


if __name__ == "__main__":
    unittest.main(verbosity=2)
