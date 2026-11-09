import sys
import unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experimental"))
from skill_system.task_routing import PRIORITY
from skill_system.transport import PROFILES

class ModelConfigAdvertisement(unittest.TestCase):
    def test_current_auto_routes_are_supported(self):
        self.assertEqual(PRIORITY, ("Gwarden_Gemini4Argon", "InkStone_AgentsA1"))
        self.assertTrue(all(route in PROFILES for route in PRIORITY))
        self.assertFalse(any("Omnirush" in route or "Logicc" in route for route in PRIORITY))
