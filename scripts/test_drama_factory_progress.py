import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from pathlib import Path
from unittest.mock import patch

from drama_factory.core import write_json
from drama_factory.progress import INTERVAL_SECONDS, resource_snapshot, watch


class ProgressTests(unittest.TestCase):
    def test_factory_pushes_waiting_and_generation_without_a_provider_call(self):
        self.assertEqual(INTERVAL_SECONDS, 10 * 60)
        with tempfile.TemporaryDirectory() as directory:
            resource = Path(directory) / 'resources.json'
            write_json(resource, {'state': 'allocating', 'started_at': 1})
            write_json(resource.with_name('allocation-request.json'), {'events': [{'status': 503}]})
            notified = threading.Event()
            messages = []

            def notify(_, message):
                messages.append(message)
                if len(messages) == 1:
                    write_json(resource, {'state': 'ready', 'progress_phase': 'generating',
                                          'progress_job': '003', 'completed_jobs': ['002']})
                else:
                    notified.set()

            stderr = io.StringIO()
            with patch('drama_factory.progress._notify', side_effect=notify), redirect_stderr(stderr):
                with watch('补拍', directory, lambda: [resource_snapshot(resource, 8)], interval=0.01):
                    self.assertTrue(notified.wait(2))
            events = [json.loads(line) for line in
                      resource.with_name('progress-events.jsonl').read_text().splitlines()]
            self.assertEqual(events[0]['batches'][0]['phase'], 'waiting_for_gpu')
            self.assertEqual(events[0]['batches'][0]['provider_http_status'], 503)
            self.assertEqual(events[1]['batches'][0]['collected'], 1)
            self.assertIn('003', messages[1])
            self.assertIn('生成中', stderr.getvalue())
