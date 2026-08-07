import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from RaspberryPi.backend.services.cell_config_service import CellConfigError, CellConfigStore
from RaspberryPi.backend.storage.sqlite.sqlite_storage import SQLiteStorage


BOUNDS = {
    "temperature": {
        "green": [{"min": 1, "max": 4}],
        "yellow": [{"min": 4, "max": 8}],
        "red": [{"min": -20, "max": 1}, {"min": 8, "max": 100}],
    },
    "humidity": {
        "green": [{"min": 80, "max": 90}],
        "yellow": [{"min": 70, "max": 80}, {"min": 90, "max": 95}],
        "red": [{"min": 0, "max": 70}, {"min": 95, "max": 100}],
    },
}


class ProductProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.categories = root / "categories.json"
        self.categories.write_text(
            json.dumps(
                {
                    "categories": {
                        "food": {
                            "name": "Food",
                            **BOUNDS,
                            "voc_policy": {"warning_score": 25, "alert_score": 50},
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        self.storage = SQLiteStorage(str(root / "test.db"))
        self.storage.start()
        self.store = CellConfigStore(
            self.storage,
            seed_config_path=root / "missing.json",
            categories_path=self.categories,
        )
        self.store.load()

    def tearDown(self):
        self.storage.stop()
        self.temp.cleanup()

    def test_category_seed_is_idempotent_and_legacy_category_maps_linked(self):
        self.assertIsNotNone(self.store.get_profile("food"))
        self.store.set_category(3073, "food")
        cell = self.store.get_cell_config(3073)
        self.assertEqual(cell["assignment_mode"], "linked")
        self.assertEqual(cell["profile_id"], "food")

        reloaded = CellConfigStore(
            self.storage,
            seed_config_path=Path(self.temp.name) / "missing.json",
            categories_path=self.categories,
        )
        reloaded.load()
        self.assertEqual(len(reloaded.list_profiles()), 1)
        self.assertEqual(reloaded.get_cell_config(3073)["assignment_mode"], "linked")

    def test_link_propagates_and_override_merges(self):
        profile = self.store.create_profile(
            {"profile_id": "produce", "name": "Produce", **deepcopy(BOUNDS)}
        )
        self.store.set_assignment(3073, "linked", profile_id=profile["profile_id"])
        self.store.set_assignment(
            3165,
            "override",
            profile_id=profile["profile_id"],
            voc_policy_override={"warning_score": 30, "warning_clear_score": 22},
        )

        self.store.update_profile(
            profile["profile_id"],
            {"voc_policy": {"warning_score": 28, "warning_clear_score": 21, "alert_score": 60}},
        )
        linked = self.store.get_cell_config(3073)
        overridden = self.store.get_cell_config(3165)
        self.assertEqual(linked["voc_policy"]["warning_score"], 28)
        self.assertEqual(overridden["voc_policy"]["warning_score"], 30)
        self.assertEqual(overridden["voc_policy"]["alert_score"], 60)

        persisted = self.storage.load_cell_configs()
        self.assertEqual(persisted[3073]["voc_policy"]["warning_score"], 28)
        self.assertEqual(persisted[3165]["voc_policy_override"]["warning_score"], 30)

    def test_copied_assignment_is_rejected(self):
        profile = self.store.create_profile(
            {"profile_id": "produce", "name": "Produce", **deepcopy(BOUNDS)}
        )
        with self.assertRaises(CellConfigError):
            self.store.set_assignment(3073, "copied", profile_id=profile["profile_id"])

    def test_profiles_omit_voc_index_bounds(self):
        profile = self.store.create_profile(
            {"profile_id": "no-gas", "name": "No Gas", **deepcopy(BOUNDS)}
        )
        self.assertNotIn("gas", profile)
        self.assertIn("voc_policy", profile)
        self.assertEqual(profile["voc_policy"]["warning_score"], 25)

        listed = self.store.list_profiles()
        self.assertTrue(all("gas" not in item for item in listed))

    def test_delete_rejects_live_reference_but_allows_after_custom(self):
        profile = self.store.create_profile(
            {"profile_id": "link-source", "name": "Link", **deepcopy(BOUNDS)}
        )
        self.store.set_assignment(3073, "linked", profile_id=profile["profile_id"])
        with self.assertRaises(CellConfigError):
            self.store.delete_profile(profile["profile_id"])
        self.store.set_assignment(
            3073,
            "custom",
            custom_bounds={
                "temperature": {
                    "yellow_low": -20,
                    "green_low": 1,
                    "green_high": 4,
                    "yellow_high": 8,
                },
                "humidity": {
                    "yellow_low": 70,
                    "green_low": 80,
                    "green_high": 90,
                    "yellow_high": 95,
                },
            },
        )
        self.store.delete_profile(profile["profile_id"])
        self.assertIsNone(self.store.get_profile(profile["profile_id"]))
        self.assertEqual(self.store.get_cell_config(3073)["profile_source"], "custom")


if __name__ == "__main__":
    unittest.main()
