import json
import tempfile
import unittest
from pathlib import Path

from RaspberryPi.backend.services.cell_config_service import (
    VOC_SCORE_DURATION_SECONDS,
    CellConfigError,
    CellConfigStore,
)
from RaspberryPi.backend.services.trigger_service import TriggerService
from RaspberryPi.backend.storage.sqlite.sqlite_storage import SQLiteStorage


class RecordingNotifier:
    def __init__(self):
        self.voc_events = []

    async def send_alert(self, **kwargs):
        return True

    async def send_voc_alert(self, **kwargs):
        self.voc_events.append(kwargs)
        return True


class VocPolicyTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        categories = root / "categories.json"
        categories.write_text(json.dumps({"categories": {}}), encoding="utf-8")
        self.storage = SQLiteStorage(str(root / "test.db"))
        self.storage.start()
        self.config = CellConfigStore(
            self.storage,
            seed_config_path=root / "missing.json",
            categories_path=categories,
        )
        self.config.load()
        self.config.set_voc_policy(
            3073,
            {
                "warning_score": 25,
                "alert_score": 50,
                "critical_score": 75,
            },
        )
        self.notifier = RecordingNotifier()
        self.trigger = TriggerService(self.config, self.notifier, storage=self.storage)

    async def asyncTearDown(self):
        self.storage.stop()
        self.temp.cleanup()

    @staticmethod
    def node(**overrides):
        value = {
            "node_id": 3073,
            "protocol_version": 5,
            "autonomous_state": 5,
            "cell_empty": False,
            "measurement_usable": True,
            "voc_change_score": 55,
            "voc_6sigma": True,
            "voc_10sigma": True,
            "temperature_c": None,
            "humidity_rh": None,
            "voc_index": 100,
        }
        value.update(overrides)
        return value

    async def test_continuous_policy_latches_and_only_empty_clears(self):
        await self.trigger.tick([self.node()], now=100)
        await self.trigger.tick([self.node()], now=100 + VOC_SCORE_DURATION_SECONDS)
        self.assertEqual([event["severity"] for event in self.notifier.voc_events], ["warning", "alert"])
        self.assertTrue(self.storage.is_voc_alert_latched(3073))
        self.assertEqual(
            self.trigger.get_voc_status(3073, now=100 + VOC_SCORE_DURATION_SECONDS)["voc_decision_state"],
            "alert",
        )

        await self.trigger.tick(
            [self.node(measurement_usable=False, environment_fault=True)],
            now=100 + VOC_SCORE_DURATION_SECONDS + 1,
        )
        await self.trigger.tick([], now=100 + VOC_SCORE_DURATION_SECONDS + 10)
        self.assertTrue(self.storage.is_voc_alert_latched(3073))

        await self.trigger.tick(
            [self.node(autonomous_state=2, cell_empty=True, measurement_usable=False)],
            now=100 + VOC_SCORE_DURATION_SECONDS + 20,
        )
        self.assertFalse(self.storage.is_voc_alert_latched(3073))
        self.assertEqual(
            self.trigger.get_voc_status(
                3073, now=100 + VOC_SCORE_DURATION_SECONDS + 20
            )["voc_decision_state"],
            "monitoring",
        )

    async def test_unusable_evidence_resets_pending_timer(self):
        await self.trigger.tick([self.node()], now=1)
        await self.trigger.tick([self.node(measurement_usable=False)], now=900)
        await self.trigger.tick([self.node()], now=901)
        await self.trigger.tick([self.node()], now=901 + VOC_SCORE_DURATION_SECONDS - 1)
        self.assertEqual(self.notifier.voc_events, [])
        await self.trigger.tick([self.node()], now=901 + VOC_SCORE_DURATION_SECONDS)
        self.assertEqual(self.notifier.voc_events[0]["severity"], "warning")

    async def test_policy_rejects_inverted_thresholds_and_mutable_sigma_gates(self):
        with self.assertRaises(CellConfigError):
            self.config.set_voc_policy(
                3073,
                {
                    "warning_score": 60,
                    "alert_score": 50,
                    "critical_score": 75,
                },
            )
        with self.assertRaises(CellConfigError):
            self.config.set_voc_policy(
                3073,
                {
                    "warning_score": 25,
                    "alert_score": 80,
                    "critical_score": 75,
                },
            )
        with self.assertRaises(CellConfigError):
            self.config.set_voc_policy(
                3073,
                {
                    "warning_score": 25,
                    "warning_sigma": 10,
                    "alert_score": 50,
                    "critical_score": 75,
                },
            )

    async def test_policy_forces_fixed_durations_and_critical(self):
        policy = self.config.set_voc_policy(
            3073,
            {
                "warning_score": 25,
                "warning_seconds": 10,
                "alert_score": 50,
                "alert_seconds": 20,
                "critical_score": 80,
                "critical_seconds": 5,
            },
        )["voc_policy"]
        self.assertEqual(policy["warning_seconds"], VOC_SCORE_DURATION_SECONDS)
        self.assertEqual(policy["alert_seconds"], VOC_SCORE_DURATION_SECONDS)
        self.assertEqual(policy["critical_seconds"], VOC_SCORE_DURATION_SECONDS)
        self.assertEqual(policy["critical_score"], 80)

    async def test_warning_hysteresis_requires_continuous_clear_duration(self):
        self.config.set_voc_policy(
            3073,
            {
                "warning_score": 25,
                "alert_score": 90,
                "critical_score": 95,
            },
        )
        await self.trigger.tick([self.node(voc_change_score=30, voc_10sigma=False)], now=0)
        await self.trigger.tick(
            [self.node(voc_change_score=30, voc_10sigma=False)],
            now=VOC_SCORE_DURATION_SECONDS,
        )
        self.assertTrue(
            self.trigger.get_voc_status(3073, now=VOC_SCORE_DURATION_SECONDS)["warning_active"]
        )

        clear_seconds = self.config.get_effective_voc_policy(3073)["warning_clear_seconds"]
        clear_start = VOC_SCORE_DURATION_SECONDS + 1
        await self.trigger.tick(
            [self.node(voc_change_score=20, voc_10sigma=False)],
            now=clear_start,
        )
        await self.trigger.tick(
            [self.node(voc_change_score=21, voc_10sigma=False)],
            now=clear_start + clear_seconds - 1,
        )
        resume = clear_start + clear_seconds
        await self.trigger.tick(
            [self.node(voc_change_score=20, voc_10sigma=False)],
            now=resume,
        )
        await self.trigger.tick(
            [self.node(voc_change_score=20, voc_10sigma=False)],
            now=resume + clear_seconds,
        )
        self.assertFalse(
            self.trigger.get_voc_status(3073, now=resume + clear_seconds)["warning_active"]
        )

    async def test_critical_escalates_alert_and_survives_restart(self):
        self.config.set_voc_policy(
            3073,
            {
                "warning_score": 25,
                "alert_score": 50,
                "critical_score": 80,
            },
        )
        critical = self.node(voc_change_score=85)
        await self.trigger.tick([critical], now=100)
        await self.trigger.tick([critical], now=100 + VOC_SCORE_DURATION_SECONDS)
        status = self.trigger.get_voc_status(3073, now=100 + VOC_SCORE_DURATION_SECONDS)
        self.assertEqual(status["voc_latch_severity"], "critical")
        self.assertTrue(status["voc_critical_latched"])

        restarted = TriggerService(self.config, self.notifier, storage=self.storage)
        restored = restarted.get_voc_status(3073, now=100 + VOC_SCORE_DURATION_SECONDS + 1)
        self.assertEqual(restored["evaluation_state"], "critical")
        self.assertEqual(restored["voc_latch_severity"], "critical")


if __name__ == "__main__":
    unittest.main()
