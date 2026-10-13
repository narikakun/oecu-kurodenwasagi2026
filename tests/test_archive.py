import asyncio
import unittest
from unittest.mock import patch

from kuro_sagi_denwa.archive import TranscriptArchive


class TranscriptArchiveTest(unittest.IsolatedAsyncioTestCase):
    async def test_queues_call_and_transcript_events_in_order(self):
        archive = TranscriptArchive(
            "https://example.com/ingest.php", "secret", "phone-1"
        )

        call_id = archive.begin_call()
        archive.record_transcript("assistant", "もしもし？")
        archive.record_transcript("user", "もしもし。")
        archive.end_call()

        events = []
        while not archive._queue.empty():
            events.append(archive._queue.get_nowait())

        self.assertIsNotNone(call_id)
        self.assertEqual(
            [event["action"] for event in events],
            ["call_started", "transcript", "transcript", "call_ended"],
        )
        self.assertTrue(all(event["call_id"] == call_id for event in events))
        self.assertEqual(events[0]["device_name"], "phone-1")
        self.assertEqual(events[1]["speaker"], "assistant")
        self.assertEqual(events[2]["speaker"], "user")
        self.assertEqual(events[-1]["status"], "completed")
        self.assertIsNone(archive.current_call_id)

    async def test_worker_posts_without_blocking_calling_code(self):
        archive = TranscriptArchive(
            "https://example.com/ingest.php", "secret", "phone-1"
        )
        posted = []
        archive._post = posted.append
        archive.start()

        archive.begin_call()
        archive.record_transcript("assistant", "テストです。")
        archive.end_call()
        await asyncio.wait_for(archive._queue.join(), timeout=1)
        await archive.stop()

        self.assertEqual(
            [event["action"] for event in posted],
            ["call_started", "transcript", "call_ended"],
        )

    async def test_disabled_archive_does_not_queue_events(self):
        archive = TranscriptArchive("", "", "phone-1")

        self.assertIsNone(archive.begin_call())
        archive.record_transcript("user", "送られません")
        archive.end_call()

        self.assertTrue(archive._queue.empty())

    async def test_post_uses_bearer_token_and_requires_json_success(self):
        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self, size):
                return b'{"ok":true}'

        class Opener:
            def __init__(self):
                self.request = None

            def open(self, request, timeout):
                self.request = request
                self.timeout = timeout
                return Response()

        opener = Opener()
        archive = TranscriptArchive(
            "https://example.com/ingest.php", "secret-token", "phone-1"
        )
        with patch("urllib.request.build_opener", return_value=opener):
            archive._post({"action": "test"})

        self.assertEqual(opener.request.get_header("Authorization"), "Bearer secret-token")
        self.assertEqual(opener.timeout, 10)


if __name__ == "__main__":
    unittest.main()

