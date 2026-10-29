import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoeq as A  # noqa: E402

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None


def sine(freq, amp, fs, seconds, phase=0.0):
    return [amp * math.sin(2 * math.pi * freq * k / fs + phase) for k in range(int(fs * seconds))]


class FilterTests(unittest.TestCase):
    def test_coefficients_match_the_app(self):
        # w0 = 0.39269908169872414 (pi/8 = 2*pi*500/8000), Q = 1/sqrt(2)
        b0, b1, b2, a1, a2 = A.lpf_coefficients()
        w0 = 0.39269908169872414
        alpha = math.sin(w0) / (2 / math.sqrt(2))
        a0 = 1 + alpha
        self.assertAlmostEqual(b0, (1 - math.cos(w0)) / 2 / a0, places=12)
        self.assertAlmostEqual(b1, (1 - math.cos(w0)) / a0, places=12)
        self.assertAlmostEqual(a1, -2 * math.cos(w0) / a0, places=12)
        self.assertAlmostEqual(a2, (1 - alpha) / a0, places=12)

    def test_unity_gain_at_dc(self):
        b0, b1, b2, a1, a2 = A.lpf_coefficients()
        self.assertAlmostEqual((b0 + b1 + b2) / (1 + a1 + a2), 1.0, places=12)


class SpectrumTests(unittest.TestCase):
    @unittest.skipIf(np is None, "numpy not installed")
    def test_frame_spectrum_matches_numpy(self):
        rng = np.random.default_rng(7)
        buf = list(rng.uniform(-0.5, 0.5, A.BUFFER))
        ours = np.array(A.frame_spectrum(buf))
        idx = np.arange(A.FFT_N) * (A.APP_RATE / A.FFT_N)
        i = idx.astype(int)
        frac = idx - i
        b = np.array(buf)
        x = (b[i + 1] * frac + b[i] * (1 - frac)) * np.blackman(A.FFT_N)
        ref = 20 * np.log10(np.abs(np.fft.fft(x))[: A.FFT_N // 2] / (A.FFT_N // 2))
        self.assertLess(float(np.max(np.abs(ours - ref))), 1e-9)

    def test_bins_are_one_hertz(self):
        for f in (20, 52, 160):
            spec = A.frame_spectrum(sine(f, 0.5, A.APP_RATE, 1.0))
            self.assertEqual(max(range(1, 300), key=lambda k: spec[k]), f)

    def test_buffer_length_enforced(self):
        with self.assertRaises(ValueError):
            A.frame_spectrum([0.0] * 100)


class AnalyzerTests(unittest.TestCase):
    def test_sine_level_and_bands_at_48k(self):
        an = A.Analyzer(48000)
        an.feed(sine(52, 0.5, 48000, 2.5))
        expected = 20 * math.log10(0.42 * 0.5)  # Blackman coherent gain 0.42, |X|/(N/2)
        self.assertAlmostEqual(an.max_hold[52], expected, delta=0.1)
        bands = dict(zip(A.BANDS_HZ, an.bands()))
        self.assertAlmostEqual(bands[52], an.max_hold[52])
        self.assertLess(bands[20], bands[52] - 20)  # far from the tone: much lower

    def test_waits_for_full_buffer_then_every_hop(self):
        an = A.Analyzer(8000)
        an.feed([0.0] * A.BUFFER)
        self.assertEqual(an.frames, 0)  # app: callback only once more than 8000 samples arrived
        an.feed([0.0] * A.HOP)
        self.assertEqual(an.frames, 1)
        an.feed([0.0] * (A.HOP * 3))
        self.assertEqual(an.frames, 4)

    def test_max_hold_only_while_holding(self):
        an = A.Analyzer(8000)
        an.holding = False
        an.feed(sine(60, 0.5, 8000, 2))
        self.assertEqual(an.max_hold[60], A.FLOOR_DB)
        an.holding = True
        an.feed(sine(60, 0.5, 8000, 0.5))
        self.assertGreater(an.max_hold[60], -20)
        an.reset_hold()
        self.assertEqual(an.max_hold[60], A.FLOOR_DB)

    def test_rejects_unsupported_rate(self):
        with self.assertRaises(ValueError):
            A.Analyzer(44100)


class CombineRunsTests(unittest.TestCase):
    def test_single_run_is_unchanged(self):
        r = A.combine_runs([[-20.0, -30.0]], "same")
        self.assertEqual(r["levels"], [-20.0, -30.0])
        self.assertEqual(r["warn"], [])

    def test_median_removes_one_disturbed_run(self):
        # second run caught a door slam in band 1
        r = A.combine_runs([[-20.0, -30.0], [-20.4, -12.0], [-19.8, -30.6]], "same")
        self.assertAlmostEqual(r["levels"][0], -20.0)
        self.assertAlmostEqual(r["levels"][1], -30.3)  # median of the two runs kept
        self.assertEqual(r["dropped"], [0, 1])
        self.assertEqual(r["warn"], [])

    def test_moved_runs_are_power_averaged(self):
        r = A.combine_runs([[-20.0], [-26.0], [-23.0]], "moved")
        expect = 10 * math.log10((10 ** -2 + 10 ** -2.6 + 10 ** -2.3) / 3)
        self.assertAlmostEqual(r["levels"][0], expect, places=9)
        self.assertEqual(r["dropped"], [0])
        self.assertAlmostEqual(r["spread"][0], 6.0)

    def test_moved_outlier_dropped_before_averaging(self):
        r = A.combine_runs([[-20.0], [-21.0], [-5.0]], "moved")
        self.assertEqual(r["dropped"], [1])
        self.assertLess(r["levels"][0], -19.0)

    def test_spread_warning_when_runs_disagree(self):
        r = A.combine_runs([[-20.0], [-24.0]], "same")  # two runs: nothing can be dropped
        self.assertEqual(r["warn"], [0])

    def test_bands_without_data_are_ignored(self):
        r = A.combine_runs([[-1000.0, -20.0], [-1000.0, -22.0], [-21.0, -21.0]], "same")
        self.assertEqual(r["levels"][0], -21.0)
        self.assertEqual(A.combine_runs([[-1000.0]], "same")["levels"], [A.FLOOR_DB])

    def test_validation(self):
        with self.assertRaises(ValueError):
            A.combine_runs([], "same")
        with self.assertRaises(ValueError):
            A.combine_runs([[1.0]], "sideways")
        with self.assertRaises(ValueError):
            A.combine_runs([[1.0], [1.0, 2.0]], "same")


class ProtocolTests(unittest.TestCase):
    def test_encode_measurement_layout(self):
        payload = A.encode_measurement(A.STEP_FAR, [-13.55] + [0.0] * 15)
        self.assertEqual(len(payload), 2 + 32)
        self.assertEqual(payload[:2], bytes((0, 1)))
        self.assertEqual(int.from_bytes(payload[2:4], "big", signed=True), -135)  # Java (short)(x*10): truncation

    def test_unmeasured_band_matches_app_value(self):
        payload = A.encode_measurement(A.STEP_NEAR, [A.FLOOR_DB] * 16)
        self.assertEqual(int.from_bytes(payload[2:4], "big", signed=True), -10000)

    def test_encode_validates(self):
        with self.assertRaises(ValueError):
            A.encode_measurement(2, [0.0] * 16)
        with self.assertRaises(ValueError):
            A.encode_measurement(0, [0.0] * 15)

    def test_decode_correction(self):
        values = list(range(-80, 80, 10))
        payload = bytes((0, 1)) + b"".join(v.to_bytes(2, "big", signed=True) for v in values)
        self.assertEqual(A.decode_correction(payload), [v / 10 for v in values])
        with self.assertRaises(ValueError):
            A.decode_correction(bytes(10))

    def test_generator_state(self):
        self.assertEqual(A.generator_state(bytes((0, 1, 2))), A.GEN_DONE)
        self.assertIsNone(A.generator_state(bytes((0, 1))))

    def test_bands_are_the_apps(self):
        self.assertEqual(A.BANDS_HZ, (20, 23, 26, 30, 34, 39, 45, 52, 60, 69, 79, 91, 105, 121, 139, 160))


if __name__ == "__main__":
    unittest.main()
