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
        self.assertIn("function renderSettings(event)", html)
        self.assertIn("settings-subtitle", html)
        self.assertIn("settings-footer", html)
        self.assertIn(".frame.status-mode #status { display:flex; }", html)
        self.assertIn("statusBox.textContent = '・・・'", html)

    def test_uses_line_seed_jp_font(self):
        html = (
            Path(__file__).parents[1]
            / "src"
            / "kuro_sagi_denwa"
            / "assets"
            / "display"
            / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn("family=LINE+Seed+JP:wght@400;700", html)
        self.assertIn('font-family:"LINE Seed JP"', html)

    def test_feedback_mode_shows_warning_image_with_transcripts(self):
        asset_directory = (
            Path(__file__).parents[1]
            / "src"
            / "kuro_sagi_denwa"
            / "assets"
            / "display"
        )
        html = (asset_directory / "index.html").read_text(encoding="utf-8")

        self.assertTrue((asset_directory / "refund-fraud-warning.png").is_file())
        self.assertIn(".frame.feedback-mode #feedback { display:flex; }", html)
        self.assertIn(".frame.feedback-mode #messages", html)
        self.assertIn("event.type === 'feedback'", html)
        self.assertIn("messages.replaceChildren(); partials.clear();", html)
        self.assertIn("refund-fraud-warning.png", html)


if __name__ == "__main__":
    unittest.main()
