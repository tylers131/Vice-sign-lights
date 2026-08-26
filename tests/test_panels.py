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


if __name__ == "__main__":
    unittest.main(verbosity=2)
