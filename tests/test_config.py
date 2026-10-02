import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kuro_sagi_denwa.config import Settings, load_env_file


class LoadEnvFileTest(unittest.TestCase):
    def test_loads_api_key_from_env_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# コメント\nOPENAI_API_KEY='test-secret'\nLIVE_MODEL=gpt-live-1\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {}, clear=True):
                self.assertTrue(load_env_file(path))
                settings = Settings.from_env()
                self.assertEqual(settings.api_key, "test-secret")
                self.assertEqual(settings.live_model, "gpt-live-1")

    def test_existing_environment_variable_has_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("OPENAI_API_KEY=from-file\n", encoding="utf-8")
            with patch.dict(os.environ, {"OPENAI_API_KEY": "from-shell"}, clear=True):
                load_env_file(path)
                self.assertEqual(os.environ["OPENAI_API_KEY"], "from-shell")

    def test_missing_file_is_not_an_error(self):
        self.assertFalse(load_env_file("/path/that/does/not/exist/.env"))


if __name__ == "__main__":
    unittest.main()
