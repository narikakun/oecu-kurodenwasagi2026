import asyncio
import unittest

from kuro_sagi_denwa.transcript import TranscriptLogger


class TranscriptLoggerTest(unittest.IsolatedAsyncioTestCase):
    async def test_fragments_are_printed_as_one_line(self):
        transcripts = TranscriptLogger(flush_seconds=0.01)

        with self.assertLogs("conversation", level="INFO") as logs:
            transcripts.add("assistant", "もし")
            transcripts.add("assistant", "もし。")
            await asyncio.sleep(0.02)

        self.assertEqual(logs.output, ["INFO:conversation:AI: もしもし。"])

    async def test_user_and_assistant_are_buffered_separately(self):
        transcripts = TranscriptLogger(flush_seconds=1)

        with self.assertLogs("conversation", level="INFO") as logs:
            transcripts.add("user", "はい")
            transcripts.add("assistant", "確認します")
            await transcripts.flush_all()

        self.assertIn("INFO:conversation:参加者: はい", logs.output)
        self.assertIn("INFO:conversation:AI: 確認します", logs.output)

    async def test_empty_fragments_are_ignored(self):
        transcripts = TranscriptLogger(flush_seconds=0.01)
        transcripts.add("assistant", "")
        await transcripts.flush_all()


if __name__ == "__main__":
    unittest.main()
