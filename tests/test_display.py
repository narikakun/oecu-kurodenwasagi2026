import unittest
from pathlib import Path

from kuro_sagi_denwa.display import DisplayServer


class DisplayServerTest(unittest.TestCase):
    def test_reset_discards_previous_display_history(self):
        display = DisplayServer()
        display.publish({"type": "transcript", "text": "古い会話"})

        display.reset()
        display.publish({"type": "state", "state": "idle", "label": "待機中"})

        self.assertEqual(
            display._events,
            [
                {"type": "reset"},
                {"type": "state", "state": "idle", "label": "待機中"},
            ],
        )

    def test_settings_events_switch_to_a_dedicated_screen(self):
        html = (
            Path(__file__).parents[1]
            / "src"
            / "kuro_sagi_denwa"
            / "assets"
            / "display"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('.frame.settings-mode #settings { display:flex; }', html)
        self.assertIn('.frame.settings-mode #messages { display:none; }', html)
        self.assertIn("frame.classList.add('settings-mode')", html)


if __name__ == "__main__":
    unittest.main()
