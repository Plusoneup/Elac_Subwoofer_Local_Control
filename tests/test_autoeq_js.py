"""The browser's AutoEQ analyser must give the same numbers as core/autoeq.py (the port of the ELAC app).

Needs Playwright with Chromium; skipped otherwise. Loads the page from disk, no server needed.
"""
import math
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import autoeq as A  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None

PAGE = Path(__file__).resolve().parents[1] / "static" / "index.html"


def test_signal(fs, seconds):
    """Three tones plus deterministic pseudo-noise (an LCG, so JS and Python get identical input)."""
    out, seed = [], 12345
    for k in range(int(fs * seconds)):
        seed = (seed * 1103515245 + 12345) % 2 ** 31
        noise = (seed / 2 ** 31 - 0.5) * 0.02
        t = k / fs
        out.append(0.3 * math.sin(2 * math.pi * 31 * t) + 0.2 * math.sin(2 * math.pi * 79.5 * t) + 0.1 * math.sin(2 * math.pi * 140 * t) + noise)
    return out


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class JsAnalyzerMatchesPython(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:  # pragma: no cover
            cls.pw.stop()
            raise unittest.SkipTest(f"chromium unavailable: {exc}")
        cls.page = cls.browser.new_page()
        cls.page.goto(PAGE.as_uri())

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def run_js(self, fs, samples):
        return self.page.evaluate(
            """([fs, data]) => { const an = new window.__elacAeq.AeqAnalyzer(fs);
                 for (let i = 0; i < data.length; i += 4096) an.feed(Float32Array.from(data.slice(i, i + 4096)));
                 return {hold: Array.from(an.hold), bands: an.bands(), frames: an.frames}; }""",
            [fs, samples])

    def test_same_max_hold_and_bands_at_48k(self):
        samples = test_signal(48000, 2.5)
        py = A.Analyzer(48000)
        # JS feeds Float32Array (what a browser delivers); round the Python input the same way
        import struct
        f32 = [struct.unpack("f", struct.pack("f", v))[0] for v in samples]
        py.feed(f32)
        js = self.run_js(48000, samples)
        self.assertEqual(js["frames"], py.frames)
        worst = max(abs(a - b) for a, b in zip(js["hold"][1:400], py.max_hold[1:400]))
        self.assertLess(worst, 1e-6)
        for a, b in zip(js["bands"], py.bands()):
            self.assertAlmostEqual(a, b, places=6)

    def test_bands_list_matches(self):
        self.assertEqual(self.page.evaluate("window.__elacAeq.AEQ_BANDS"), list(A.BANDS_HZ))

    def test_44k1_input_is_resampled_sensibly(self):
        # Not bit-exact (44.1 kHz is resampled), but tones must land on the right bin at the right level.
        js = self.run_js(44100, [0.5 * math.sin(2 * math.pi * 52 * k / 44100) for k in range(int(44100 * 2.5))])
        hold = js["hold"]
        self.assertEqual(max(range(1, 300), key=lambda k: hold[k]), 52)
        self.assertAlmostEqual(hold[52], 20 * math.log10(0.42 * 0.5), delta=0.3)

    def test_combine_runs_matches_python(self):
        import random
        rnd = random.Random(7)
        cases = []
        for n in (1, 2, 3, 5):
            runs = [[rnd.uniform(-60, -10) for _ in A.BANDS_HZ] for _ in range(n)]
            if n >= 3:
                runs[1][4] += 15.0      # a disturbance in one run
            runs[0][0] = A.FLOOR_DB     # a band without data
            cases.append(runs)
        for runs in cases:
            for mode in ("same", "moved"):
                py = A.combine_runs(runs, mode)
                js = self.page.evaluate("([r, m]) => window.__elacAeq.aeqCombine(r, m)", [runs, mode])
                self.assertEqual(js["dropped"], py["dropped"])
                self.assertEqual(js["warn"], py["warn"])
                for a, b in zip(js["levels"], py["levels"]):
                    self.assertAlmostEqual(a, b, places=9)
                for a, b in zip(js["spread"], py["spread"]):
                    self.assertAlmostEqual(a, b, places=9)


if __name__ == "__main__":
    unittest.main()
