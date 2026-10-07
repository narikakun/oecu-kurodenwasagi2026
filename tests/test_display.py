import unittest

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


if __name__ == "__main__":
    unittest.main()
