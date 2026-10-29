import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "core"))
import unittest

from elac_sub_control_commands import (
    query_0049, set_0024_text, set_0049_filter_record, set_004b_scaled,
    set_u8, startup_requests,
)
from elac_sub_control_protocol import (
    FRAME_DELIMITER,
    ESCAPE,
    ESCAPED_DELIMITER,
    ESCAPED_ESCAPE,
    FrameStreamDecoder,
    PacketError,
    crc16_xmodem,
    decode_frame,
    decode_filter_record,
    encode_filter_record,
    encode_frame,
)


class ProtocolTests(unittest.TestCase):
    def test_crc_known_xmodem_vector(self):
        self.assertEqual(crc16_xmodem(b"123456789"), 0x31C3)
        self.assertEqual(crc16_xmodem(b""), 0x0000)

    def test_simple_packet_round_trip(self):
        frame = encode_frame("0022", 1, 0, b"\x02")
        packet = decode_frame(frame)
        self.assertEqual(packet.command_id, "0022")
        self.assertEqual(packet.command_type, 1)
        self.assertEqual(packet.status, 0)
        self.assertEqual(packet.payload, b"\x02")
        self.assertTrue(packet.crc_ok)

    def test_reserved_bytes_are_stuffed(self):
        payload = bytes((0x00, FRAME_DELIMITER, ESCAPE, 0x7F))
        frame = encode_frame("0048", 1, 0, payload)
        self.assertIn(bytes((ESCAPE, ESCAPED_DELIMITER)), frame)
        self.assertIn(bytes((ESCAPE, ESCAPED_ESCAPE)), frame)
        packet = decode_frame(frame)
        self.assertEqual(packet.payload, payload)
        self.assertTrue(packet.crc_ok)

    def test_crc_error_is_reported(self):
        frame = bytearray(encode_frame("0023", 1, 0, b"\x20"))
        # Change a non-delimiter body byte, choosing the command type byte.
        frame[3] ^= 0x01
        packet = decode_frame(bytes(frame))
        self.assertFalse(packet.crc_ok)

    def test_stream_decoder_handles_split_and_combined_frames(self):
        first = encode_frame("0022", 0, 0, b"")
        second = encode_frame("0023", 1, 0, b"\x14")
        decoder = FrameStreamDecoder()
        self.assertEqual(decoder.feed(first[:3]), [])
        out = decoder.feed(first[3:] + second)
        self.assertEqual([p.command_id for p in out], ["0022", "0023"])
        self.assertTrue(all(p.crc_ok for p in out))

    def test_known_empty_request_frame(self):
        self.assertEqual(encode_frame("0022", 0, 0, b""),
                         bytes.fromhex("C0 00 22 00 00 E8 A6 C0"))

    def test_startup_sequence_has_22_requests_in_observed_order(self):
        frames = startup_requests()
        packets = [decode_frame(frame) for frame in frames]
        self.assertEqual(len(packets), 22)
        self.assertEqual([p.command_id for p in packets[:2]], ["0040", "004A"])
        query_0049_packets = [p for p in packets if p.command_id == "0049"]
        self.assertEqual([p.payload for p in query_0049_packets],
                         [bytes((0, i)) for i in (0, 1, *range(8, 16))])
        self.assertTrue(all(p.command_type == 0 and p.status == 0 and p.crc_ok for p in packets))
        self.assertEqual([p.command_id for p in packets[-10:]],
                         ["004B", "0022", "0023", "0172", "0170", "0024", "0001", "0002", "0005", "01D0"])

    def test_command_builders_match_observed_shapes(self):
        self.assertEqual(decode_frame(set_u8("0022", 2)).payload, b"\x02")
        self.assertEqual(decode_frame(query_0049(8)).payload, b"\x00\x08")
        self.assertEqual(decode_frame(set_0024_text("local name")).payload, b"local name\x00")
        self.assertEqual(decode_frame(set_0049_filter_record(bytes(9))).payload, bytes(9))
        self.assertEqual(decode_frame(set_004b_scaled(12)).payload, b"\x00\x78")

    def test_invalid_filter_record_length_rejected(self):
        with self.assertRaises(ValueError):
            set_0049_filter_record(bytes(8))

    def test_filter_record_signed_fields_and_scaling(self):
        raw = encode_filter_record(1, 2, 50, 0.5, -2.0)
        self.assertEqual(raw, bytes.fromhex("00 01 02 01 F4 00 32 FF EC"))
        decoded = decode_filter_record(raw)
        self.assertEqual(decoded.index, 1)
        self.assertEqual(decoded.filter_type, 2)
        self.assertEqual(decoded.frequency_cutoff, 50)
        self.assertEqual(decoded.quality, 0.5)
        self.assertEqual(decoded.gain, -2.0)
        self.assertEqual(decoded.raw, raw)

    def test_filter_record_matches_java_truncation_for_negative_values(self):
        # Encoded -19 in the x10 int16 field; Java -19 / 10 truncates to -1.
        raw = bytes.fromhex("00 01 02 FF ED 00 00 00 00")
        self.assertEqual(decode_filter_record(raw).frequency_cutoff, -1)

    def test_command_id_must_be_two_bytes_as_hex(self):
        with self.assertRaises(PacketError):
            encode_frame("022", 1, 0, b"")


if __name__ == "__main__":
    unittest.main()
