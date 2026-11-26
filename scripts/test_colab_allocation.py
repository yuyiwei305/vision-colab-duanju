import importlib.util
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from drama_factory import colab_allocation as allocation
from drama_factory.core import FactoryError, read_json, write_json


class FakeTransport:
    session = 'test-gpu'

    def __init__(self, replies):
        self.replies = iter(replies)
        self.calls = []

    def allocation_request(self, operation, gpu, request_id, deadline, resource):
        self.calls.append((operation, gpu, request_id, deadline))
        assert read_json(resource.with_name('allocation-request.json'))['request_id'] == request_id
        reply = next(self.replies)
        if isinstance(reply, Exception):
            raise reply
        return reply


class AllocationTests(unittest.TestCase):
    def test_retries_503_and_recovers_on_the_same_notebook(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            transport = FakeTransport([
                {'state': 'retry', 'status': 503},
                {'state': 'retry', 'status': 503},
                {'state': 'assigned', 'endpoint': 'original-vm'},
            ])
            with patch.object(allocation.time, 'time', return_value=1000), patch.object(allocation.time, 'sleep'):
                self.assertEqual(allocation.allocate(transport, state, 'G4', 3000), 'original-vm')
            self.assertEqual(len(transport.calls), 3)
            self.assertEqual(len({call[2] for call in transport.calls}), 1)
            self.assertEqual({call[3] for call in transport.calls}, {2800})

    def test_interrupted_process_recovers_the_persisted_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            original = FakeTransport([OSError('process interrupted')])
            with patch.object(allocation.time, 'time', return_value=1000):
                with self.assertRaises(OSError):
                    allocation.allocate(original, state, 'L4', 5000)
            resumed = FakeTransport([{'state': 'assigned', 'endpoint': 'original-vm'}])
            with patch.object(allocation.time, 'time', return_value=1100):
                self.assertEqual(allocation.allocate(resumed, state, 'L4', 5000), 'original-vm')
            self.assertEqual(resumed.calls, original.calls)

    def test_after_deadline_only_reconcile_the_original_request(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            transport = FakeTransport([{'state': 'retry', 'status': 503}, {'state': 'unassigned'}])
            now = [1000]
            def wait(seconds):
                now[0] += seconds
            with patch.object(allocation.time, 'time', side_effect=lambda: now[0]), \
                 patch.object(allocation.time, 'sleep', side_effect=wait):
                with self.assertRaisesRegex(FactoryError, 'colab_allocation_deadline'):
                    allocation.allocate(transport, state, 'G4', 1004)
            self.assertEqual([call[0] for call in transport.calls], ['attempt', 'lookup'])
            self.assertEqual(transport.calls[0][2], transport.calls[1][2])

    def test_permanent_rejection_stops_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            transport = FakeTransport([{'state': 'rejected', 'status': 412}])
            with self.assertRaisesRegex(FactoryError, 'colab_allocation_limit_or_capacity'):
                allocation.allocate(transport, state, 'G4', allocation.time.time() + 60)
            self.assertEqual(len(transport.calls), 1)


@unittest.skipUnless(importlib.util.find_spec('colab_cli'), 'run with installed Colab CLI Python')
class ColabClientTests(unittest.TestCase):
    def test_retry_reads_existing_assignment_and_registers_it_without_another_post(self):
        from colab_cli.client import Assignment, ColabRequestError
        request_id = 'b0c2a4f5-4edf-4fb9-8d35-936ee19dde49'
        assignment = Assignment.model_validate({
            'endpoint': 'original-vm',
            'runtimeProxyInfo': {'token': 'example', 'url': 'https://example.com', 'tokenExpiresInSeconds': 3600},
        })
        provider = Mock()
        provider.client._get_assignment.side_effect = [SimpleNamespace(token='xsrf'), assignment]
        provider.client._post_assignment.side_effect = ColabRequestError(
            'unavailable', request=None, response=SimpleNamespace(status_code=503))
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ'):
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            deadline = allocation.time.time() + 60
            self.assertEqual(allocation.request_once(provider, 'attempt', 'test', 'G4', request_id, deadline, state),
                             {'state': 'retry', 'status': 503})
            self.assertEqual(allocation.request_once(provider, 'attempt', 'test', 'G4', request_id, deadline, state),
                             {'state': 'assigned', 'endpoint': 'original-vm'})
        provider.client._post_assignment.assert_called_once()
        self.assertEqual(provider.client._get_assignment.call_args_list[0],
                         provider.client._get_assignment.call_args_list[1])
        self.assertEqual(provider.store.add.call_args.args[0].endpoint, 'original-vm')

    def test_operator_stop_during_preflight_never_sends_post(self):
        request_id = 'b0c2a4f5-4edf-4fb9-8d35-936ee19dde49'
        provider = Mock()
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ'):
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            def stop_before_post(*args):
                write_json(state, {'state': 'release_requested'})
                return SimpleNamespace(token='xsrf')
            provider.client._get_assignment.side_effect = stop_before_post
            result = allocation.request_once(provider, 'attempt', 'test', 'G4', request_id,
                                             allocation.time.time() + 60, state)
        self.assertEqual(result, {'state': 'retry', 'reason': 'stopped'})
        provider.client._post_assignment.assert_not_called()
