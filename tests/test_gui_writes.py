"""Browser test: the Controls card writes every changed value (volume, LPF, phase, delay, Auto-On) with one button.

Serves static/ from a throw-away HTTP server and answers /api/* in the browser, so no device or FastAPI is needed.
"""
import json
import sys
import threading
import unittest
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core.demo_snapshot import demo_snapshot  # noqa: E402

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # pragma: no cover
    sync_playwright = None


class _Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class ControlsWriteTests(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(ROOT)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_one_button_writes_only_the_changed_controls(self):
        snap = demo_snapshot("AA:BB:CC:DD:EE:FF")
        snap["scalars"]["0023"] = {"command_id": "0023", "value": 6, "threshold_mv": 6}
        writes, dialogs = [], []

        def api(route, request):
            path = request.url.split(self.base, 1)[1]
            if path.startswith("/api/status"):
                return route.fulfill(json={"connected": True, "address": snap["address"], "demo": False, "auth": False})
            if path == "/api/read":
                return route.fulfill(json=snap)
            if path == "/api/write":
                writes.append(json.loads(request.post_data))
                return route.fulfill(json={"ok": True})
            return route.fulfill(json={})

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # browser binary missing
                self.skipTest(f"chromium unavailable: {exc}")
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.add_init_script("try{localStorage.setItem('elac_gui_lang','en');localStorage.setItem('elac_gui_collapsed_v1','[]')}catch(e){}")
            page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
            page.route("**/api/**", api)
            page.goto(self.base + "/static/index.html")
            page.wait_for_function("document.getElementById('autoOnThresholdEntry').value==='6'", timeout=8000)
            self.assertFalse(page.is_visible("#writeBar"), "no bar without changes")
            page.click("#writeModeBtn")
            delay_plus = "#control-card .knob:has(#delayEntry) .stp[data-step='1']"
            start = int(page.input_value("#delayEntry"))
            for _ in range(3):
                page.click(delay_plus)  # − / + steppers
            self.assertEqual(page.input_value("#delay"), str(start + 3), "the slider follows the stepper")
            page.fill("#autoOnThresholdEntry", "3")
            page.dispatch_event("#autoOnThresholdEntry", "change")
            self.assertTrue(page.is_visible("#writeBar"))
            self.assertIn("2", page.text_content("#wbCount"))
            page.evaluate("""()=>{window.__spin=false;new MutationObserver(()=>{if(document.querySelector('#wbWrite .busyspin'))window.__spin=true}).observe(document.getElementById('wbWrite'),{childList:true,subtree:true})}""")
            page.click("#wbWrite")
            page.wait_for_timeout(500)
            self.assertTrue(page.evaluate("window.__spin"), "the write button shows the saving animation")
            page.wait_for_function("document.getElementById('wbWrite').textContent==='Write'", timeout=5000)
            page.click(delay_plus)  # a new local change after the write...
            page.click("#wbRevert")  # ...is undone back to the last read
            undo_state = page.input_value("#delayEntry")
            browser.close()
        self.assertEqual([(w["kind"], w["value"]) for w in writes], [("delay", start + 3), ("autoon_threshold", 3)])
        confirm = dialogs[-1]
        self.assertIn("Auto-On: 6 mV → 3 mV", confirm)
        self.assertNotIn("LPF", confirm)
        self.assertEqual(undo_state, str(start))  # the stand-in device still reports the old delay


    def test_bluetooth_pill_runs_connect_and_read(self):
        snap = demo_snapshot("AA:BB:CC:DD:EE:FF")
        hits = []

        def api(route, request):
            path = request.url.split(self.base, 1)[1]
            hits.append(path)
            if path.startswith("/api/status"):
                return route.fulfill(json={"connected": True, "address": snap["address"], "demo": False, "auth": False})
            if path == "/api/read":
                return route.fulfill(json=snap)
            return route.fulfill(json={})

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # browser binary missing
                self.skipTest(f"chromium unavailable: {exc}")
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.route("**/api/**", api)
            page.goto(self.base + "/static/index.html")
            page.wait_for_function("document.querySelector('.status .dot').style.background.includes('accent')", timeout=8000)
            page.wait_for_timeout(300)
            before = hits.count("/api/read")
            page.click("#connStatus")
            page.wait_for_timeout(500)
            after_click = hits.count("/api/read")
            page.focus("#connStatus")
            page.keyboard.press("Enter")
            page.wait_for_timeout(500)
            after_key = hits.count("/api/read")
            browser.close()
        self.assertGreater(after_click, before)
        self.assertGreater(after_key, after_click)

    def test_peq_edit_goes_through_the_write_bar(self):
        snap = demo_snapshot("AA:BB:CC:DD:EE:FF")
        writes = []

        def api(route, request):
            path = request.url.split(self.base, 1)[1]
            if path.startswith("/api/status"):
                return route.fulfill(json={"connected": True, "address": snap["address"], "demo": False, "auth": False})
            if path == "/api/read":
                return route.fulfill(json=snap)
            if path == "/api/write":
                writes.append(json.loads(request.post_data))
                return route.fulfill(json={"ok": True})
            return route.fulfill(json={})

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # browser binary missing
                self.skipTest(f"chromium unavailable: {exc}")
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.add_init_script("try{localStorage.setItem('elac_gui_lang','en');localStorage.setItem('elac_gui_collapsed_v1','[]')}catch(e){}")
            page.on("dialog", lambda d: d.accept())
            page.route("**/api/**", api)
            page.goto(self.base + "/static/index.html")
            page.wait_for_function("document.getElementById('peqGain').value==='3'", timeout=8000)  # PEQ 1 of the demo
            page.click("#writeModeBtn")
            page.click(".peq-row:has(#peqGain) .stp[data-step='1']")  # +0.5 dB
            self.assertEqual(page.input_value("#peqGain"), "3.5")
            self.assertTrue(page.is_visible("#writeBar"))
            self.assertIn("edited", page.get_attribute("[data-filter-index='8']", "class"))
            page.click("#wbWrite")
            page.wait_for_timeout(400)
            browser.close()
        self.assertEqual(writes, [{"enabled": True, "kind": "peq", "index": 8, "frequency_hz": 31.5, "q": 4.0, "gain_db": 3.5}])


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class AutoEqRunsTests(unittest.TestCase):
    """Device-mode AutoEQ with three runs: the generator runs three times, one combined upload follows."""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(ROOT)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _wav(self, amplitude):
        import math, struct, tempfile, wave
        fd = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        fd.close()
        self.addCleanup(lambda: Path(fd.name).unlink(missing_ok=True))
        with wave.open(fd.name, "wb") as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(48000)
            w.writeframes(b"".join(struct.pack("<h", int(32767 * amplitude * math.sin(2 * math.pi * 45 * k / 48000))) for k in range(48000 * 2)))
        return fd.name

    def _run_near(self, audio_file):
        snap = demo_snapshot("AA:BB:CC:DD:EE:FF")
        calls, gen = [], {"polls": 0}

        def api(route, request):
            path = request.url.split(self.base, 1)[1]
            body = json.loads(request.post_data) if request.post_data else {}
            if path.startswith("/api/status"):
                return route.fulfill(json={"connected": True, "address": snap["address"], "demo": False, "auth": False,
                                           "autoeq_device": True, "autoeq_experimental": True})
            if path == "/api/read":
                return route.fulfill(json=snap)
            if path.startswith("/api/autoeq/"):
                name = path.rsplit("/", 1)[1]
                calls.append((name, body))
                if name == "start":
                    gen["polls"] = 0
                if name == "state":
                    gen["polls"] += 1
                    return route.fulfill(json={"state": 1 if gen["polls"] < 3 else 2})
                if name == "upload":
                    return route.fulfill(json={"ok": True, "correction_db": None})
                return route.fulfill(json={"ok": True})
            return route.fulfill(json={})

        with sync_playwright() as p:
            try:
                args = ["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream"]
                if audio_file:
                    args.append(f"--use-file-for-fake-audio-capture={audio_file}")
                browser = p.chromium.launch(args=args)
            except Exception as exc:  # browser binary missing
                self.skipTest(f"chromium unavailable: {exc}")
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, permissions=["microphone"])
            page = ctx.new_page()
            page.add_init_script("try{localStorage.setItem('elac_gui_lang','en');localStorage.setItem('elac_gui_collapsed_v1','[]');localStorage.setItem('elac_gui_aeq_runs','3')}catch(e){}")
            page.on("dialog", lambda d: d.accept())
            page.route("**/api/**", api)
            page.goto(self.base + "/static/index.html")
            page.wait_for_function("document.querySelector('.status .dot').style.background.includes('accent')", timeout=8000)
            page.click("#writeModeBtn")
            self.assertEqual(page.input_value("#aeqRuns"), "3")
            page.click("#aeqNearBtn")
            page.wait_for_function("document.getElementById('aeqStepNear').classList.contains('done')||/AutoEQ:/.test(document.getElementById('aeqStatus').textContent)", timeout=40000)
            status = page.text_content("#aeqStatus")
            dashed = page.locator("#aeqChart polyline.aeq-run").count()
            self.console = page.text_content("#aeqLog")
            self.stages = page.evaluate("Object.fromEntries([...document.querySelectorAll('#aeqSteps [data-stage]')].map(e=>[e.dataset.stage,e.dataset.state]))")
            browser.close()
        return calls, status, dashed

    def test_three_runs_one_upload(self):
        calls, status, dashed = self._run_near(self._wav(0.3))
        names = [c[0] for c in calls]
        self.assertNotIn("AutoEQ:", status)
        self.assertEqual(names.count("start"), 3, names)
        self.assertEqual(names.count("stop"), 3, names)
        uploads = [b for n, b in calls if n == "upload"]
        self.assertEqual(len(uploads), 1)
        self.assertEqual(uploads[0]["step"], 0)
        self.assertEqual(len(uploads[0]["levels"]), 16)
        self.assertEqual(dashed, 3, "each run is drawn as a thin line")
        for text in ("near field · runs: 3", "run 3/3 done", "combined: median of 3", "sent · subwoofer confirmed"):
            self.assertIn(text, self.console)
        self.assertEqual(self.stages, {"near": "ok", "sent": "ok", "seat": "todo", "cal": "todo"})

    def test_clipping_stops_before_upload(self):
        calls, status, _ = self._run_near(self._wav(1.0))  # full-scale input clips
        self.assertIn("clipped in run 1", status)
        self.assertIn("clipped in run 1", self.console)
        self.assertEqual(self.stages["near"], "fail")
        self.assertEqual([c[0] for c in calls if c[0] == "upload"], [])
        self.assertIn("stop", [c[0] for c in calls])  # the generator is stopped


    def test_verify_switches_autoeq_off_then_back_on(self):
        snap = demo_snapshot("AA:BB:CC:DD:EE:FF")
        snap["scalars"]["0170"] = {"command_id": "0170", "value": True}
        snap["scalars"]["0172"] = {"command_id": "0172", "value": True}
        calls, gen = [], {"polls": 0}

        def api(route, request):
            path = request.url.split(self.base, 1)[1]
            body = json.loads(request.post_data) if request.post_data else {}
            if path.startswith("/api/status"):
                return route.fulfill(json={"connected": True, "address": snap["address"], "demo": False, "auth": False,
                                           "autoeq_device": True, "autoeq_experimental": True})
            if path == "/api/read":
                return route.fulfill(json=snap)
            if path == "/api/write":
                calls.append(("write", body.get("kind"), body.get("value")))
                return route.fulfill(json={"ok": True})
            if path.startswith("/api/autoeq/"):
                name = path.rsplit("/", 1)[1]
                calls.append((name, body.get("step")))
                if name == "start":
                    gen["polls"] = 0
                if name == "state":
                    gen["polls"] += 1
                    return route.fulfill(json={"state": 1 if gen["polls"] < 3 else 2})
                return route.fulfill(json={"ok": True})
            return route.fulfill(json={})

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(args=["--use-fake-device-for-media-stream", "--use-fake-ui-for-media-stream",
                                                  f"--use-file-for-fake-audio-capture={self._wav(0.3)}"])
            except Exception as exc:  # browser binary missing
                self.skipTest(f"chromium unavailable: {exc}")
            page = browser.new_context(viewport={"width": 1280, "height": 900}, permissions=["microphone"]).new_page()
            page.add_init_script("try{localStorage.setItem('elac_gui_lang','en');localStorage.setItem('elac_gui_collapsed_v1','[]');localStorage.setItem('elac_gui_aeq_runs','1')}catch(e){}")
            page.on("dialog", lambda d: d.accept())
            page.route("**/api/**", api)
            page.goto(self.base + "/static/index.html")
            page.wait_for_function("document.querySelector('.status .dot').style.background.includes('accent')", timeout=8000)
            page.click("#writeModeBtn")
            page.click("#aeqVerifyBtn")
            page.wait_for_function("!document.getElementById('aeqVerifyBtn').disabled&&document.getElementById('aeqStepVerify').classList.contains('done')", timeout=60000)
            status = page.text_content("#aeqStatus")
            log = page.text_content("#aeqLog")
            effect = page.text_content("#aeqCorrTitle") if page.is_visible("#aeqCorrWrap") else ""
            browser.close()
        writes = [c for c in calls if c[0] == "write"]
        self.assertEqual(writes, [("write", "autoeq_enabled", 0), ("write", "autoeq_enabled", 1)])
        self.assertEqual([c for c in calls if c[0] == "start"], [("start", 1), ("start", 1)])
        self.assertNotIn("upload", [c[0] for c in calls])
        self.assertIn("AutoEQ switched off", log)
        self.assertIn("AutoEQ switched on", log)
        self.assertIn("on minus off", effect)
        self.assertTrue(any(k in status for k in ("No measurable difference", "flatter", "not flatter")), status)



@unittest.skipIf(sync_playwright is None, "playwright not installed")
class SeveralSubwoofersTests(unittest.TestCase):
    """No device chosen: one subwoofer found is picked by itself; with two the user chooses; the sheet switches."""

    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), partial(_Quiet, directory=str(ROOT)))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _run(self, state, found, actions):
        calls = []

        def api(route, request):
            path = request.url.split(self.base, 1)[1]
            body = json.loads(request.post_data) if request.post_data else {}
            if path.startswith("/api/status"):
                return route.fulfill(json={"connected": bool(state["address"]), "address": state["address"], "demo": False,
                                           "auth": False, "devices": state["devices"]})
            calls.append((path, body))
            if path == "/api/scan":
                return route.fulfill(json={"devices": found})
            if path == "/api/address":
                state["address"] = body["address"]
                if not any(d["address"] == body["address"] for d in state["devices"]):
                    state["devices"].append({"address": body["address"], "name": ""})
                return route.fulfill(json={"address": body["address"], "devices": state["devices"]})
            if path == "/api/read":
                return route.fulfill(json=demo_snapshot(state["address"] or "AA:BB:CC:DD:EE:FF"))
            return route.fulfill(json={})

        with sync_playwright() as p:
            try:
                browser = p.chromium.launch()
            except Exception as exc:  # browser binary missing
                self.skipTest(f"chromium unavailable: {exc}")
            page = browser.new_page(viewport={"width": 1280, "height": 900})
            page.add_init_script("try{localStorage.setItem('elac_gui_lang','en');localStorage.setItem('elac_gui_collapsed_v1','[]')}catch(e){}")
            page.route("**/api/**", api)
            page.goto(self.base + "/static/index.html")
            page.wait_for_timeout(1200)
            result = actions(page)
            browser.close()
        return calls, result

    SUB = {"name": "SUB3030", "rssi": -50, "likely": True}

    def test_only_subwoofer_found_is_picked_and_read(self):
        found = [dict(self.SUB, address="AA:BB:CC:00:00:01"), {"name": "TV", "address": "AA:BB:CC:00:00:09", "rssi": -40, "likely": False}]
        calls, _ = self._run({"address": "", "devices": []}, found, lambda page: None)
        paths = [c[0] for c in calls]
        self.assertIn(("/api/address", {"address": "AA:BB:CC:00:00:01"}), calls)
        self.assertIn("/api/read", paths[paths.index("/api/address"):])

    def test_two_subwoofers_wait_for_a_choice(self):
        found = [dict(self.SUB, address="AA:BB:CC:00:00:01"), dict(self.SUB, address="AA:BB:CC:00:00:02", rssi=-70)]
        calls, shown = self._run({"address": "", "devices": []}, found,
                                 lambda page: page.locator("#scanResults .scan-item").count())
        self.assertNotIn("/api/address", [c[0] for c in calls])
        self.assertEqual(shown, 2)

    def test_device_sheet_switches_to_the_other_subwoofer(self):
        state = {"address": "AA:BB:CC:00:00:01",
                 "devices": [{"address": "AA:BB:CC:00:00:01", "name": "Living room"}, {"address": "AA:BB:CC:00:00:02", "name": "Cinema"}]}

        def act(page):
            title = page.text_content("#brandName")
            page.click("#deviceSheetBtn")
            page.click("#devList .dev-row:not(.current) .dev-go")
            page.wait_for_timeout(800)
            return title, page.text_content("#brandName")

        calls, (before, after) = self._run(state, [], act)
        self.assertEqual(before, "Living room")
        self.assertIn(("/api/address", {"address": "AA:BB:CC:00:00:02"}), calls)
        self.assertEqual(after, "Cinema")


if __name__ == "__main__":
    unittest.main()
