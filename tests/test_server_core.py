import asyncio
import json
import sys
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server_core as sc  # noqa: E402
from core.demo_snapshot import demo_snapshot  # noqa: E402

HOSTS = sc.default_allowed_hosts()


class SecurityTests(unittest.TestCase):
    def test_local_get_allowed(self):
        self.assertIsNone(sc.check_request("GET", "127.0.0.1:8765", None, HOSTS))
        self.assertIsNone(sc.check_request("GET", "localhost:8765", None, HOSTS))
        self.assertIsNone(sc.check_request("GET", "[::1]:8765", None, HOSTS))

    def test_unknown_host_blocked_even_for_get(self):  # DNS rebinding
        self.assertIsNotNone(sc.check_request("GET", "evil.example:8765", None, HOSTS))
        self.assertIsNotNone(sc.check_request("GET", None, None, HOSTS))

    def test_extra_hosts_from_env(self):
        hosts = sc.default_allowed_hosts("sub.home.lan, Other.Lan")
        self.assertIsNone(sc.check_request("POST", "sub.home.lan:8765", "http://sub.home.lan:8765", hosts))
        self.assertIsNone(sc.check_request("GET", "other.lan", None, hosts))

    def test_post_same_origin_ok_cross_origin_blocked(self):
        self.assertIsNone(sc.check_request("POST", "127.0.0.1:8765", "http://127.0.0.1:8765", HOSTS, "same-origin"))
        self.assertIsNone(sc.check_request("POST", "127.0.0.1:8765", None, HOSTS))  # curl, scripts
        self.assertIsNotNone(sc.check_request("POST", "127.0.0.1:8765", "https://evil.example", HOSTS))
        self.assertIsNotNone(sc.check_request("POST", "127.0.0.1:8765", "http://localhost:8765", HOSTS))  # different origin
        self.assertIsNotNone(sc.check_request("POST", "127.0.0.1:8765", "http://127.0.0.1:3000", HOSTS))  # other local app
        self.assertIsNotNone(sc.check_request("POST", "127.0.0.1:8765", "null", HOSTS))
        self.assertIsNotNone(sc.check_request("POST", "127.0.0.1:8765", None, HOSTS, "cross-site"))
        self.assertIsNotNone(sc.check_request("POST", "127.0.0.1:8765", None, HOSTS, "same-site"))


class SettingsTests(unittest.TestCase):
    def test_defaults(self):
        with tempfile.TemporaryDirectory() as d:
            s = sc.Settings(d, {})
            self.assertEqual(s.address, sc.DEFAULT_ADDRESS)
            self.assertFalse(s.demo)
            self.assertFalse(s.address_locked)
            self.assertEqual(s.idle_timeout_s, 300.0)

    def test_save_and_reload_address(self):
        with tempfile.TemporaryDirectory() as d:
            sc.Settings(d, {}).save_address("AA:BB:CC:DD:EE:FF")
            self.assertEqual(sc.Settings(d, {}).address, "AA:BB:CC:DD:EE:FF")

    def test_env_wins_and_locks(self):
        with tempfile.TemporaryDirectory() as d:
            sc.Settings(d, {}).save_address("AA:BB:CC:DD:EE:FF")
            s = sc.Settings(d, {"ELAC_ADDRESS": "11:22:33:44:55:66", "DEMO": "1", "ELAC_IDLE_DISCONNECT_S": "0"})
            self.assertEqual(s.address, "11:22:33:44:55:66")
            self.assertTrue(s.address_locked and s.demo)
            self.assertEqual(s.idle_timeout_s, 0.0)

    def test_corrupt_config_falls_back(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "config.json").write_text("{not json", encoding="utf-8")
            self.assertEqual(sc.Settings(d, {}).address, sc.DEFAULT_ADDRESS)
            (Path(d) / "config.json").write_text(json.dumps({"address": "garbage"}), encoding="utf-8")
            self.assertEqual(sc.Settings(d, {}).address, sc.DEFAULT_ADDRESS)

    def test_address_validation(self):
        self.assertEqual(sc.normalize_address("aa-bb-cc-dd-ee-ff"), "AA:BB:CC:DD:EE:FF")
        self.assertEqual(sc.normalize_address("123e4567-e89b-12d3-a456-426614174000"), "123E4567-E89B-12D3-A456-426614174000")
        for bad in ("", "nope", "AA:BB:CC:DD:EE", "AA:BB:CC:DD:EE:GG", None):
            with self.assertRaises(sc.AppError):
                sc.normalize_address(bad)


class ScanTests(unittest.TestCase):
    def test_ranking_and_filtering(self):
        entries = [
            sc.describe_advert("A1", None, -40, []),                       # anonymous noise: dropped
            sc.describe_advert("A2", "Headphones", -50, []),
            sc.describe_advert("A3", "ELAC SUB3030", -80, []),             # likely by name
            sc.describe_advert("A4", "", -60, [sc.SERVICE.upper()]),       # likely by service, anonymous
        ]
        ranked = [e["address"] for e in sc.rank_scan(entries)]
        self.assertEqual(ranked, ["A4", "A3", "A2"])


class CountingDemo(sc.DemoBLE):
    def __init__(self):
        super().__init__()
        self.snapshots = 0
        self.busy = False
        self.overlap = False

    async def snapshot(self):
        self.snapshots += 1
        self.busy = True
        await asyncio.sleep(0.05)
        self.busy = False
        return demo_snapshot(self.address)

    async def close(self):
        if self.busy:
            self.overlap = True
        await super().close()


class OperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_reads_share_one_snapshot(self):
        ble = CountingDemo()
        results = await asyncio.gather(*(ble.op_read() for _ in range(5)))
        self.assertEqual(ble.snapshots, 1)
        self.assertTrue(all(r is results[0] for r in results))
        await ble.op_read()  # a later read does a fresh one
        self.assertEqual(ble.snapshots, 2)

    async def test_operations_do_not_interleave(self):
        ble = CountingDemo()
        await asyncio.gather(ble.op_read(), ble.op_disconnect(), ble.op_read())
        self.assertFalse(ble.overlap)

    async def test_failed_read_is_raised_to_every_waiter_and_next_read_retries(self):
        ble = CountingDemo()

        async def boom():
            await asyncio.sleep(0.02)
            raise sc.AppError("x", "pl", "en")
        ble.snapshot = boom
        outcomes = await asyncio.gather(ble.op_read(), ble.op_read(), return_exceptions=True)
        self.assertTrue(all(isinstance(o, sc.AppError) for o in outcomes))
        ble.snapshot = CountingDemo.snapshot.__get__(ble)
        self.assertIn("scalars", await ble.op_read())


class FakeLink(sc.ElacBLE):
    """Real snapshot() logic with scripted per-command results."""

    def __init__(self, failing=(), link_drops_on=None):
        super().__init__()
        self.failing, self.link_drops_on, self.up, self.calls = set(failing), link_drops_on, True, []

    @property
    def connected(self):
        return self.up

    async def scalar(self, cmd):
        return await self._do(cmd, lambda: {"command_id": cmd, "value": 1})

    async def filt(self, index):
        return await self._do(f"filter:{index}", lambda: {"index": index, "frequency_hz": 50, "q": 5, "gain_db": 0})

    async def _do(self, key, ok):
        self.calls.append(key)
        if key == self.link_drops_on:
            self.up = False
            raise RuntimeError("write failed")
        if key in self.failing or "*" in self.failing:
            raise sc.AppError("timeout", f"pl {key}", f"en {key}")
        return ok()


class WriteValidationTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_confirmed_scalar_setters_are_allowed(self):
        link = sc.ElacBLE()
        with self.assertRaises(sc.AppError) as cm:
            await link.write_scalar("0022", 1)
        self.assertEqual(cm.exception.code, "write_not_allowed")
        with self.assertRaises(sc.AppError) as cm:
            await link.write_scalar("0040", 101)
        self.assertEqual(cm.exception.code, "bad_value")

    async def test_autoon_threshold_range(self):
        # Confirmed on hardware: raw = mV, app range 1-10 mV.
        link = sc.ElacBLE()
        for bad in (0, 11, -1):
            with self.assertRaises(sc.AppError) as cm:
                await link.write_scalar("0023", bad)
            self.assertEqual(cm.exception.code, "bad_value")
        fake = FakeLink()
        with self.assertRaises(sc.AppError) as cm:
            await fake.op_write_setting({"kind": "autoon_threshold", "value": 4.5, "enabled": True})
        self.assertEqual(cm.exception.code, "bad_value")

    async def test_power_mode_is_not_writable(self):
        # SUB3030 has no user-selectable mode; on older subs 0022 switched Manual/Auto/12 V.
        with self.assertRaises(sc.AppError) as cm:
            await sc.ElacBLE().write_scalar("0022", 1)
        self.assertEqual(cm.exception.code, "write_not_allowed")

    async def test_filter_write_type_is_allow_listed(self):
        link = sc.ElacBLE()
        with self.assertRaises(sc.AppError) as cm:
            await link.write_filter(0, 80, 0.71, 0.0, 20)
        self.assertEqual(cm.exception.code, "write_not_allowed")
        with self.assertRaises(sc.AppError) as cm:
            await link.write_filter(2, 80, 0.71, 0.0, 9)
        self.assertEqual(cm.exception.code, "write_not_allowed")


class SnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_clean_read(self):
        snap = await FakeLink().snapshot()
        self.assertEqual(len(snap["scalars"]), len(sc.SCALAR_COMMANDS))
        self.assertEqual(len(snap["filters"]), len(sc.FILTER_INDEXES))
        self.assertEqual(snap["errors"], {})
        self.assertFalse(snap["partial"])
        self.assertFalse(snap["read_only"])
        self.assertTrue(snap["write_supported"])

    async def test_isolated_failures_give_partial_result(self):
        snap = await FakeLink(failing={"0022", "filter:9"}).snapshot()
        self.assertTrue(snap["partial"])
        self.assertEqual(set(snap["errors"]), {"0022", "filter:9"})
        self.assertEqual(snap["errors"]["0022"], "en 0022")
        self.assertNotIn("0022", snap["scalars"])
        self.assertNotIn("9", snap["filters"])
        self.assertIn("0040", snap["scalars"])

    async def test_aborts_after_consecutive_failures(self):
        link = FakeLink(failing={"*"})
        with self.assertRaises(sc.AppError) as cm:
            await link.snapshot()
        self.assertEqual(cm.exception.code, "read_failed")
        self.assertEqual(len(link.calls), sc.MAX_CONSECUTIVE_FAILURES)

    async def test_failure_streak_resets_on_success(self):
        failing = {sc.SCALAR_COMMANDS[0], sc.SCALAR_COMMANDS[1], sc.SCALAR_COMMANDS[3], sc.SCALAR_COMMANDS[4]}
        snap = await FakeLink(failing=failing).snapshot()  # two pairs, never three in a row
        self.assertEqual(len(snap["errors"]), 4)

    async def test_lost_link_aborts_immediately(self):
        link = FakeLink(link_drops_on="004A")
        with self.assertRaises(sc.LinkError):
            await link.snapshot()
        self.assertEqual(link.calls, ["0040", "004A"])


class ServiceTests(unittest.IsolatedAsyncioTestCase):
    def make(self, env=None):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return sc.Service(sc.Settings(d.name, {"DEMO": "1", **(env or {})}))

    async def test_write_requires_explicit_enable_and_blocks_demo_mode(self):
        svc = self.make()
        with self.assertRaises(sc.AppError) as cm:
            await svc.write_setting({"kind": "volume", "value": 50, "enabled": False})
        self.assertEqual(cm.exception.code, "writes_disabled")
        with self.assertRaises(sc.AppError) as cm:
            await svc.write_setting({"kind": "volume", "value": 50, "enabled": True})
        self.assertEqual(cm.exception.code, "demo_write_blocked")

    async def test_demo_flow(self):
        svc = self.make()
        self.assertTrue(svc.status()["demo"])
        self.assertFalse(svc.status()["connected"])
        snap = await svc.read()
        self.assertTrue(svc.status()["connected"])
        self.assertEqual(snap["scalars"]["0040"]["value"], 62)
        self.assertEqual(sorted(int(k) for k in snap["filters"]), list(sc.FILTER_INDEXES))
        self.assertEqual((await svc.disconnect())["connected"], False)
        self.assertFalse(svc.status()["connected"])
        found = (await svc.scan())["devices"]
        self.assertTrue(found[0]["likely"])

    async def test_set_address_persists_and_respects_lock(self):
        svc = self.make()
        self.assertEqual((await svc.set_address("aa-bb-cc-dd-ee-ff"))["address"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(svc.ble.address, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(sc.Settings(svc.settings.config_path.parent, {}).address, "AA:BB:CC:DD:EE:FF")
        with self.assertRaises(sc.AppError):
            await svc.set_address("nope")
        locked = self.make({"ELAC_ADDRESS": "11:22:33:44:55:66"})
        with self.assertRaises(sc.AppError) as cm:
            await locked.set_address("AA:BB:CC:DD:EE:FF")
        self.assertEqual(cm.exception.code, "address_locked")
        self.assertEqual(locked.ble.address, "11:22:33:44:55:66")

    async def test_idle_disconnect(self):
        svc = self.make({"ELAC_IDLE_DISCONNECT_S": "60"})
        await svc.read()
        self.assertFalse(await svc.idle_check())                    # just used
        svc.ble.last_activity = time.monotonic() - 120
        async with svc.ble.op_lock:
            self.assertFalse(await svc.idle_check())                # an operation is running
        self.assertTrue(await svc.idle_check())                     # idle: dropped
        self.assertFalse(svc.ble.connected)
        self.assertFalse(await svc.idle_check())                    # nothing left to drop

    async def test_idle_disabled(self):
        svc = self.make({"ELAC_IDLE_DISCONNECT_S": "0"})
        await svc.read()
        svc.ble.last_activity = time.monotonic() - 10_000
        self.assertFalse(await svc.idle_check())
        self.assertTrue(svc.ble.connected)

    def test_error_payload(self):
        self.assertEqual(sc.error_payload(sc.AppError("c", "pl", "en"))["detail_en"], "en")
        self.assertEqual(sc.error_payload(ValueError("boom"))["detail"], "boom")


class DemoSnapshotTests(unittest.TestCase):
    def test_shape_matches_real_snapshot_keys(self):
        snap = demo_snapshot("AA:BB:CC:DD:EE:FF")
        self.assertEqual(set(snap["scalars"]), set(sc.SCALAR_COMMANDS))
        self.assertEqual(set(snap["filters"]), {str(i) for i in sc.FILTER_INDEXES})
        for key, f in snap["filters"].items():
            self.assertTrue({"q", "gain_db"} <= set(f))
            if key == "1":
                self.assertIn("phase_degrees", f)
                self.assertNotIn("frequency_hz", f)
            else:
                self.assertIn("frequency_hz", f)
        self.assertEqual(snap["scalars"]["004B"]["delay_ms"], 1.0)
        self.assertEqual(snap["scalars"]["0023"]["threshold_mv"], 6)  # raw = mV (hardware check 2026-10-26)


if __name__ == "__main__":
    unittest.main()


# ------------------------------------------------------------------ AutoEQ ----
from core import autoeq as AEQ  # noqa: E402
from core.elac_sub_control_protocol import Packet  # noqa: E402


def pkt(cmd, payload, status=1):
    return Packet(cmd, 1, status, bytes(payload), 0, 0, True, b"")


class ScriptedLink(sc.ElacBLE):
    """Real send_command/_upload logic; the 'device' answers each write from a script."""

    def __init__(self, replies):
        super().__init__()
        self.replies, self.sent = list(replies), []
        link = self

        class Client:
            is_connected = True

            async def write_gatt_char(self, uuid, frame, response=True):
                link.sent.append(frame)
                for p in (link.replies.pop(0) if link.replies else []):
                    link.queue.put_nowait(p)
        self.client = Client()

    @property
    def connected(self):
        return True


class NoBuiltInAddressTests(unittest.IsolatedAsyncioTestCase):
    async def test_connect_without_a_chosen_device_asks_to_scan(self):
        link = sc.ElacBLE("")
        with self.assertRaises(sc.AppError) as cm:
            await link.connect()
        self.assertEqual(cm.exception.code, "no_address")
        self.assertIn("Scan", cm.exception.en)


class AutoEqServerTests(unittest.IsolatedAsyncioTestCase):
    def make(self, env=None):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        return sc.Service(sc.Settings(d.name, env or {}))

    async def test_device_commands_off_by_default(self):
        svc = self.make()
        self.assertFalse(svc.status()["autoeq_device"])
        with self.assertRaises(sc.AppError) as cm:
            await svc.autoeq_start({"step": 0, "enabled": True})
        self.assertEqual(cm.exception.code, "autoeq_disabled")

    async def test_experimental_flag_and_guards(self):
        svc = self.make({"ELAC_AUTOEQ_EXPERIMENTAL": "1"})
        self.assertTrue(svc.status()["autoeq_device"])
        with self.assertRaises(sc.AppError) as cm:
            await svc.autoeq_start({"step": 0, "enabled": False})
        self.assertEqual(cm.exception.code, "writes_disabled")
        with self.assertRaises(sc.AppError) as cm:
            await svc.autoeq_start({"step": 2, "enabled": True})
        self.assertEqual(cm.exception.code, "bad_value")
        for bad in ([0.0] * 15, [0.0] * 15 + [float("nan")], [0.0] * 15 + [99.0], "x"):
            with self.assertRaises(sc.AppError):
                await svc.autoeq_upload({"step": 0, "enabled": True, "levels": bad})

    async def test_demo_wizard_flow(self):
        svc = self.make({"DEMO": "1"})
        await svc.read()
        self.assertTrue(svc.status()["autoeq_device"])
        svc.ble.DEMO_GENERATOR_S = 0.05
        self.assertTrue((await svc.autoeq_start({"step": 0, "enabled": True}))["ack"])
        self.assertEqual((await svc.autoeq_state({"step": 0}))["state"], AEQ.GEN_PLAYING)
        await asyncio.sleep(0.08)
        self.assertEqual((await svc.autoeq_state({"step": 0}))["state"], AEQ.GEN_DONE)
        await svc.autoeq_stop({})
        near = [-20.0] * 16
        self.assertIsNone((await svc.autoeq_upload({"step": 0, "enabled": True, "levels": near}))["correction_db"])
        far = [-30.0] * 16
        far[7] = -22.0  # a room peak at 52 Hz
        corr = (await svc.autoeq_upload({"step": 1, "enabled": True, "levels": far}))["correction_db"]
        self.assertEqual(len(corr), 16)
        self.assertLess(corr[7], -5)  # the peak gets cut

    async def test_far_upload_skips_ack_and_echo_then_decodes_correction(self):
        far = AEQ.encode_measurement(1, [-30.0] * 16)
        corr = bytes((0, 1)) + b"".join(v.to_bytes(2, "big", signed=True) for v in range(-80, 80, 10))
        link = ScriptedLink([[pkt("0048", b"\x00\x01"), pkt("0048", far), pkt("0048", corr)]])
        self.assertEqual(await link._upload(1, far), [v / 10 for v in range(-80, 80, 10)])

    async def test_far_upload_sub3030_ack_then_calibrated_flag(self):
        # Hardware log 2026-10-27: [00 01] ack, then 0170 = 1 three seconds later, no correction values.
        far = AEQ.encode_measurement(1, [-30.0] * 16)
        link = ScriptedLink([[pkt("0048", b"\x00\x01"), pkt("0170", b"\x01", status=0)]])
        self.assertIsNone(await link._upload(1, far))
        self.assertTrue(link.last_calibrated)
        self.assertTrue(link.last_upload_ack)

    async def test_far_upload_without_any_reply_is_an_error(self):
        link = ScriptedLink([])
        with mock.patch.object(sc.asyncio, "wait_for", side_effect=asyncio.TimeoutError):
            with self.assertRaises(sc.AppError):
                await link._upload(1, AEQ.encode_measurement(1, [-30.0] * 16))

    async def test_near_upload_and_generator_need_no_reply(self):
        link = ScriptedLink([])
        self.assertIsNone(await link._upload(0, AEQ.encode_measurement(0, [-20.0] * 16)))
        self.assertFalse(await link._gen_start(0))  # no ack is fine
        self.assertTrue(all(len(w) <= sc.BLE_CHUNK for w in link.sent))
        self.assertEqual(len(link.sent), 4)  # 0048 (42 bytes -> 20+20+2) + 0174

    async def test_generator_state_from_0177(self):
        link = ScriptedLink([[pkt("0177", (0, 0, 1))], [pkt("0177", (0, 1, 2))]])
        self.assertEqual(await link._gen_state(0), AEQ.GEN_PLAYING)
        self.assertEqual(await link._gen_state(1), AEQ.GEN_DONE)

    async def test_autoeq_toggle_requires_calibration(self):
        class NotCalibrated(FakeLink):
            async def scalar(self, cmd):
                return {"command_id": cmd, "value": False if cmd == "0170" else True}
        with self.assertRaises(sc.AppError) as cm:
            await NotCalibrated().op_write_setting({"kind": "autoeq_enabled", "value": 1, "enabled": True})
        self.assertEqual(cm.exception.code, "not_calibrated")
        with self.assertRaises(sc.AppError) as cm:
            await FakeLink().op_write_setting({"kind": "autoeq_enabled", "value": 2, "enabled": True})
        self.assertEqual(cm.exception.code, "bad_value")

    async def test_autoeq_toggle_writes_and_verifies(self):
        link = ScriptedLink([[pkt("0170", b"\x01")], [pkt("0172", b"\x00")], [pkt("0172", b"\x00")]])
        result = await link.op_write_setting({"kind": "autoeq_enabled", "value": 0, "enabled": True})
        self.assertTrue(result["verified"])
        self.assertEqual(len(link.sent), 3)  # read 0170, write 0172, read 0172 back


class AutoEqFlagTests(unittest.IsolatedAsyncioTestCase):
    def make(self, env=None, root=None):
        if root is None:
            d = tempfile.TemporaryDirectory()
            self.addCleanup(d.cleanup)
            root = d.name
        return sc.Service(sc.Settings(root, env or {}, )), root

    async def test_gui_flag_persists_and_keeps_address(self):
        svc, root = self.make()
        await svc.set_address("aa:bb:cc:dd:ee:ff")
        self.assertFalse(svc.status()["autoeq_device"])
        r = svc.set_autoeq_experimental(True)
        self.assertTrue(r["autoeq_experimental"] and r["autoeq_device"])
        again, _ = self.make(root=root)  # server restart
        self.assertTrue(again.settings.autoeq_experimental)
        self.assertEqual(again.settings.address, "AA:BB:CC:DD:EE:FF")
        again.set_autoeq_experimental(False)
        self.assertFalse(sc.Settings(root, {}).autoeq_experimental)
        self.assertEqual(json.loads((Path(root) / "config.json").read_text())["address"], "AA:BB:CC:DD:EE:FF")

    async def test_env_forces_flag_on(self):
        svc, _ = self.make({"ELAC_AUTOEQ_EXPERIMENTAL": "1"})
        self.assertTrue(svc.status()["autoeq_experimental_locked"])
        with self.assertRaises(sc.AppError) as cm:
            svc.set_autoeq_experimental(False)
        self.assertEqual(cm.exception.code, "locked")
        self.assertTrue(svc.set_autoeq_experimental(True)["autoeq_experimental"])

    async def test_flag_value_validated(self):
        svc, _ = self.make()
        with self.assertRaises(sc.AppError):
            svc.set_autoeq_experimental("yes")

    async def test_flag_ignores_non_boolean_config(self):
        _, root = self.make()
        (Path(root) / "config.json").write_text(json.dumps({"autoeq_experimental": "true"}))
        self.assertFalse(sc.Settings(root, {}).autoeq_experimental)

    async def test_stop_works_with_flag_off(self):
        svc, _ = self.make({"DEMO": "1"})
        await svc.read()
        svc.settings.demo = False  # behave like real hardware with the flag off
        self.assertFalse(svc.status()["autoeq_device"])
        self.assertEqual(await svc.autoeq_stop({}), {"stopped": True})


class ChunkedWriteTests(unittest.IsolatedAsyncioTestCase):
    # The 0048 upload that failed on Windows (elac_app.log, 2026-10-26 14:42:45): one 42-byte write.
    FAILED = bytes.fromhex("c0 00 48 01 00 00 00 fe 47 fe d6 fe f4 ff 07 ff 0d ff 0e ff 1f ff 3a ff 58 ff 60 ff 25 ff 57 ff 35 ff 22 ff 2d ff 33 08 24 c0")

    def test_log_frame_matches_our_encoder(self):
        from core.elac_sub_control_protocol import encode_frame
        payload = bytes.fromhex("00 00 FE 47 FE D6 FE F4 FF 07 FF 0D FF 0E FF 1F FF 3A FF 58 FF 60 FF 25 FF 57 FF 35 FF 22 FF 2D FF 33")
        self.assertEqual(encode_frame("0048", command_type=1, status=0, payload=payload), self.FAILED)

    async def test_long_frames_are_split_into_20_byte_writes(self):
        link = ScriptedLink([])
        await link.write_frame(self.FAILED)
        self.assertEqual([len(w) for w in link.sent], [20, 20, 2])
        self.assertEqual(b"".join(link.sent), self.FAILED)

    async def test_short_frames_are_one_write(self):
        link = ScriptedLink([])
        await link.write_frame(b"\xc0" + bytes(18) + b"\xc0")
        self.assertEqual(len(link.sent), 1)


class SavedDevicesTests(unittest.IsolatedAsyncioTestCase):
    def make(self, d, env=None):
        return sc.Service(sc.Settings(d, env or {}), ble=sc.DemoBLE(""))

    async def test_chosen_subwoofers_are_remembered_with_names(self):
        with tempfile.TemporaryDirectory() as d:
            svc = self.make(d)
            self.assertEqual(svc.status()["devices"], [])
            await svc.set_address("aa:bb:cc:dd:ee:01")
            await svc.set_address("AA:BB:CC:DD:EE:02")
            svc.rename_device("AA:BB:CC:DD:EE:01", "  Living room ")
            again = sc.Settings(d, {})                       # survives a restart
            self.assertEqual(again.address, "AA:BB:CC:DD:EE:02")
            self.assertEqual(again.device_list(), [{"address": "AA:BB:CC:DD:EE:01", "name": "Living room"},
                                                   {"address": "AA:BB:CC:DD:EE:02", "name": ""}])

    async def test_forget_and_name_rules(self):
        with tempfile.TemporaryDirectory() as d:
            svc = self.make(d)
            await svc.set_address("AA:BB:CC:DD:EE:01")
            await svc.set_address("AA:BB:CC:DD:EE:02")
            with self.assertRaises(sc.AppError) as cm:
                svc.forget_device("AA:BB:CC:DD:EE:02")       # the one in use
            self.assertEqual(cm.exception.code, "device_in_use")
            self.assertEqual(svc.forget_device("AA:BB:CC:DD:EE:01")["devices"], [{"address": "AA:BB:CC:DD:EE:02", "name": ""}])
            with self.assertRaises(sc.AppError):
                svc.rename_device("AA:BB:CC:DD:EE:02", "x" * 33)
            with self.assertRaises(sc.AppError):
                svc.rename_device("AA:BB:CC:DD:EE:09", "Kitchen")
            self.assertEqual(svc.rename_device("AA:BB:CC:DD:EE:02", "Ki\u0007tchen")["devices"][0]["name"], "Kitchen")
