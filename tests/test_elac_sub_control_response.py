import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import unittest

from elac_sub_control_protocol import decode_frame, encode_frame
from elac_sub_control_response import decode_response


class ResponseDecoderTests(unittest.TestCase):
    def decode(self, cid, payload, typ=0):
        return decode_response(decode_frame(encode_frame(cid, typ, 0, payload)))

    def test_scalar_master_volume(self):
        d = self.decode("0040", b"\x4b")
        self.assertEqual((d["field"], d["value"]), ("master_volume", 75))

    def test_scalar_accessor_length_three_uses_last_byte(self):
        d = self.decode("0022", b"\x01\x02\x03")
        self.assertEqual(d["value"], 3)

    def test_scalar_accessor_signed_byte(self):
        d = self.decode("0023", b"\xff")
        self.assertEqual(d["value"], -1)

    def test_delay_short(self):
        d = self.decode("004B", b"\x00\x64")
        self.assertEqual(d["value"], 100)

    def test_string_payload_drops_last_byte(self):
        d = self.decode("0001", b"1.2.3\x00")
        self.assertEqual(d["value"], "1.2.3")

    def test_boolean_is_first_payload_byte_equal_to_one(self):
        self.assertIs(self.decode("0172", b"\x01")["value"], True)
        self.assertIs(self.decode("0170", b"\x00")["value"], False)

    def test_filter_record(self):
        d = self.decode("0049", bytes.fromhex("00 01 02 01 F4 00 32 FF EC"))
        value = d["value"]
        self.assertEqual(value["index"], 1)
        self.assertEqual(value["frequency_cutoff"], 50)
        self.assertEqual(value["quality"], 0.5)
        self.assertEqual(value["gain"], -2.0)

    def test_bad_crc_not_semantically_decoded(self):
        frame = bytearray(encode_frame("0040", 0, 0, b"\x4b"))
        frame[-2] ^= 1
        d = decode_response(decode_frame(bytes(frame)))
        self.assertFalse(d["crc_ok"])
        self.assertFalse(d["known"])
        self.assertIn("CRC mismatch", d["error"])

    def test_unknown_command_preserved(self):
        d = self.decode("FFFF", b"\x01\x02")
        self.assertFalse(d["known"])
        self.assertEqual(d["payload_hex"], "01 02")


if __name__ == "__main__":
    unittest.main()
