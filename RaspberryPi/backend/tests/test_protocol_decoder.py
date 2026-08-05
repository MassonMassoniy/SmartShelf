import struct
import unittest

from RaspberryPi.backend.ingestion.cell_occupancy import with_cell_occupancy
from RaspberryPi.backend.ingestion.protocol_decoder import decode_payload


class ProtocolDecoderTests(unittest.TestCase):
    def test_v5_uses_autonomous_layout_and_flags(self):
        payload = struct.pack(
            "<BHBhHHhBBH",
            5,
            3073,
            0x6F,
            2345,
            5678,
            321,
            112,
            51,
            5,
            42,
        )
        packet = decode_payload(payload)

        self.assertEqual(packet.voc_change_score, 51)
        self.assertEqual(packet.autonomous_state, 5)
        self.assertTrue(packet.baseline_valid)
        self.assertFalse(packet.environment_fault)
        self.assertTrue(packet.voc_6sigma)
        self.assertTrue(packet.voc_10sigma)
        self.assertTrue(packet.measurement_usable)
        self.assertIsNone(packet.nox_index)

    def test_v5_255_score_is_unavailable_and_unusable(self):
        payload = struct.pack("<BHBhHHhBBH", 5, 1, 0x0F, 2000, 5000, 100, 99, 255, 4, 1)
        packet = decode_payload(payload)
        self.assertIsNone(packet.voc_change_score)
        self.assertFalse(packet.measurement_usable)

    def test_legacy_16_byte_nox_layout_remains_supported(self):
        payload = struct.pack("<BHBhHHhhH", 4, 9, 0x07, 2100, 4500, 222, 101, 7, 88)
        packet = decode_payload(payload)
        self.assertEqual(packet.voc_index, 101)
        self.assertEqual(packet.nox_index, 7)
        self.assertIsNone(packet.voc_change_score)
        self.assertFalse(packet.measurement_usable)

    def test_v6_includes_phase_countdowns(self):
        payload = struct.pack(
            "<BHBhHHhBBHHBH",
            6,
            3073,
            0x07,
            2345,
            5678,
            50,
            10,
            255,
            1,
            7,
            180,
            3,
            240,
        )
        packet = decode_payload(payload)
        self.assertEqual(packet.protocol_version, 6)
        self.assertEqual(packet.autonomous_state, 1)
        self.assertEqual(packet.phase_remaining_sec, 180)
        self.assertEqual(packet.concurrent_state, 3)
        self.assertEqual(packet.concurrent_remaining_sec, 240)
        self.assertIsNone(packet.voc_change_score)
        self.assertFalse(packet.measurement_usable)

    def test_occupancy_uses_proximity_regardless_of_lifecycle(self):
        stabilizing_empty = with_cell_occupancy(
            {"protocol_version": 5, "autonomous_state": 1, "proximity": 5}
        )
        stabilizing_full = with_cell_occupancy(
            {"protocol_version": 5, "autonomous_state": 1, "proximity": 500}
        )
        empty_state_high_prox = with_cell_occupancy(
            {"protocol_version": 5, "autonomous_state": 2, "proximity": 999}
        )
        missing_prox = with_cell_occupancy(
            {"protocol_version": 5, "autonomous_state": 5, "proximity": None}
        )
        self.assertEqual(stabilizing_empty["cell_status"], "empty")
        self.assertTrue(stabilizing_empty["cell_empty"])
        self.assertEqual(stabilizing_full["cell_status"], "full")
        self.assertFalse(stabilizing_full["cell_empty"])
        self.assertEqual(empty_state_high_prox["cell_status"], "full")
        self.assertEqual(missing_prox["cell_status"], "unknown")


if __name__ == "__main__":
    unittest.main()
