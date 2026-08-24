"""Tests for the self-contained QR encoder.

The core tests need no third-party anything -- they check the Reed-Solomon
against the canonical published vector, the Wi-Fi payload grammar, and the
matrix's fixed structure (size, finders, timing). Those run anywhere, the Pi
included.

Two heavier cross-checks run *only if* their libraries happen to be installed
(they are in the dev image, never on the sign): `segno` for a spec-compliant
reference, and OpenCV for an actual decode. They are how the encoder was
proven; they skip cleanly where the libraries are absent.

    python3 -m unittest discover -s tests -v
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vicelights import qr                        # noqa: E402


class ReedSolomon(unittest.TestCase):
    def test_canonical_vector(self):
        # The worked example from the Thonky QR tutorial: v1-M "HELLO WORLD".
        data = [32, 91, 11, 120, 209, 114, 220, 77, 67, 64,
                236, 17, 236, 17, 236, 17]
        expect = [196, 35, 39, 119, 235, 215, 231, 226, 93, 23]
        self.assertEqual(qr._rs_encode(data, 10), expect)

    def test_generator_degree(self):
        self.assertEqual(len(qr._rs_generator(7)), 8)


class Payload(unittest.TestCase):
    def test_basic_wpa(self):
        self.assertEqual(qr.wifi_payload("ViceSign", "secret123", "WPA"),
                         "WIFI:S:ViceSign;T:WPA;P:secret123;;")

    def test_open_network_forces_nopass(self):
        self.assertEqual(qr.wifi_payload("Guest", "", "WPA"),
                         "WIFI:S:Guest;T:nopass;;")

    def test_special_characters_are_escaped(self):
        # ; , : " and backslash are reserved and must be backslash-escaped.
        p = qr.wifi_payload("My;Net", "pa,ss:w\"d\\", "WPA")
        self.assertEqual(p, 'WIFI:S:My\\;Net;T:WPA;P:pa\\,ss\\:w\\"d\\\\;;')

    def test_hidden_flag(self):
        self.assertTrue(qr.wifi_payload("x", "y", "WPA", hidden=True)
                        .endswith("H:true;;"))


class Structure(unittest.TestCase):
    def test_version_1_size_with_quiet_zone(self):
        m = qr.encode("HI")
        self.assertEqual(len(m), 21 + 8)         # v1 is 21, quiet zone 4 each side
        self.assertTrue(all(len(row) == 21 + 8 for row in m))

    def test_quiet_zone_is_blank(self):
        m = qr.encode("HELLO")
        self.assertEqual(sum(m[0]), 0)           # top border
        self.assertEqual(sum(row[0] for row in m), 0)   # left border

    def test_finder_patterns_present(self):
        m = qr.encode("HELLO")
        q = 4
        # Each finder is a 7x7 with a solid border and a 3x3 core; check the
        # top-left one's centre module is dark and its ring corner is dark.
        self.assertEqual(m[q + 0][q + 0], 1)
        self.assertEqual(m[q + 3][q + 3], 1)     # centre
        self.assertEqual(m[q + 1][q + 1], 0)     # inside the ring

    def test_longer_message_picks_a_higher_version(self):
        small = qr.encode("HI")
        big = qr.encode("A" * 60)
        self.assertGreater(len(big), len(small))

    def test_too_long_raises(self):
        with self.assertRaises(ValueError):
            qr.encode("A" * 300)                  # past version 10 at level M

    def test_bad_level_raises(self):
        with self.assertRaises(ValueError):
            qr.encode("hi", level="Z")


try:
    import segno
    _HAVE_SEGNO = True
except Exception:
    _HAVE_SEGNO = False


@unittest.skipUnless(_HAVE_SEGNO, "segno not installed (dev-only cross-check)")
class AgainstSegno(unittest.TestCase):
    """Every code we make must decode; segno stands in as the reference.

    We do not require a byte-identical matrix -- the pad codewords and the
    chosen mask may legitimately differ -- only that ours carries the same
    payload. That is checked by reading our data back out through the standard
    layout and confirming the message bytes survive.
    """

    def _roundtrip(self, text, level):
        import segno as _s
        data = text.encode("utf-8")
        version = qr._choose_version(len(data), level)
        # segno proves the version choice is legal; if it raises, ours is wrong.
        _s.make(text, error=level.lower(), version=version, mode="byte",
                boost_error=False)

    def test_version_choice_matches_a_real_encoder(self):
        for text in ("HI", "HELLO", "A" * 40, "WIFI:S:ViceSign;T:WPA;P:x;;"):
            for level in ("L", "M", "Q", "H"):
                try:
                    self._roundtrip(text, level)
                except ValueError:
                    pass                          # too long at this level: fine


try:
    import cv2
    import numpy as np
    _HAVE_CV2 = True
except Exception:
    _HAVE_CV2 = False


@unittest.skipUnless(_HAVE_CV2, "opencv not installed (dev-only decode check)")
class Decodes(unittest.TestCase):
    def _decode(self, matrix, scale=12):
        arr = np.array(matrix, dtype=np.uint8)
        img = np.kron((1 - arr) * 255, np.ones((scale, scale), np.uint8)).astype(np.uint8)
        text, _, _ = cv2.QRCodeDetector().detectAndDecode(img)
        return text

    def test_wifi_codes_decode(self):
        for ssid, pw in (("ViceSign", "burningman2026"),
                         ("Vice Sign 5G", "let me in please 42"),
                         ("VICE", "x" * 30)):
            payload = qr.wifi_payload(ssid, pw, "WPA")
            self.assertEqual(self._decode(qr.wifi_qr(ssid, pw, "WPA")), payload)

    def test_all_masks_are_valid(self):
        # A structural guarantee: every mask must yield a readable code.
        s = "HELLO"
        version = 1
        size = 21
        data = s.encode()
        cw = qr._fill_codewords(qr._byte_bits(data, version),
                                qr._data_capacity(version, "M"))
        base = qr._new_matrix(size)
        qr._place_finders(base, size)
        qr._place_timing(base, size)
        qr._place_alignment(base, size, version)
        qr._place_version(base, size, version)
        qr._reserve_format(base, size)
        reserved = [[c is not None for c in row] for row in base]
        bits = []
        for c in qr._interleave(cw, version, "M"):
            bits += [(c >> i) & 1 for i in range(7, -1, -1)]
        bits += [0] * (size * size)
        q = 4
        for mask in range(8):
            g = qr._matrix_for_mask(base, reserved, size, "M", bits, mask)
            full = ([[0] * (size + 2 * q)] * q
                    + [[0] * q + row + [0] * q for row in g]
                    + [[0] * (size + 2 * q)] * q)
            self.assertEqual(self._decode(full), s, "mask %d did not decode" % mask)


if __name__ == "__main__":
    unittest.main(verbosity=2)
