import importlib.util
import math
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

    def test_expired_window_archives_unassigned_request_before_opening_next_window(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            now = [1000]
            transport = FakeTransport([{'state': 'retry', 'status': 503}, {'state': 'unassigned'},
                                       {'state': 'assigned', 'endpoint': 'next-window-vm'}])

            def wait(seconds):
                now[0] += 1800

            with patch.object(allocation.time, 'time', side_effect=lambda: now[0]), \
                 patch.object(allocation.time, 'sleep', side_effect=wait):
                self.assertEqual(allocation.allocate(transport, state, 'G4', 5000), 'next-window-vm')
            first, lookup, second = transport.calls
            self.assertEqual((first[0], lookup[0], second[0]), ('attempt', 'lookup', 'attempt'))
            self.assertEqual(first[2], lookup[2])
            self.assertNotEqual(first[2], second[2])
            self.assertEqual((first[3], second[3]), (2800, 4600))
            history, = read_json(state.with_name('allocation-request.json'))['previous_requests']
            self.assertEqual(history['request_id'], first[2])
            self.assertEqual(history['events'][-1]['state'], 'unassigned')

    def test_unknown_readback_does_not_open_another_window(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            write_json(state.with_name('allocation-request.json'), {
                'session': FakeTransport.session, 'gpu': 'G4', 'request_id': 'original',
                'deadline': 1000, 'attempts': 1, 'previous_requests': [],
            })
            transport = FakeTransport([{'state': 'retry', 'status': 503}])
            with patch.object(allocation.time, 'time', return_value=1001):
                with self.assertRaisesRegex(FactoryError, 'colab_allocation_lookup_unverified'):
                    allocation.allocate(transport, state, 'G4', 5000)
            self.assertEqual([call[0] for call in transport.calls], ['lookup'])
            self.assertEqual(read_json(state.with_name('allocation-request.json'))['previous_requests'], [])

    def test_ccu_only_keeps_waiting_through_capacity_and_inconclusive_readback(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'resources.json'
            write_json(state, {'state': 'allocating'})
            now = [1000]
            transport = FakeTransport([{'state': 'rejected', 'status': 412},
                                       {'state': 'retry', 'status': 503},
                                       {'state': 'unassigned'},
                                       {'state': 'assigned', 'endpoint': 'next-window-vm'}])

            def wait(seconds):
                now[0] += 3600

            with patch.object(allocation.time, 'time', side_effect=lambda: now[0]), \
                 patch.object(allocation.time, 'sleep', side_effect=wait):
                self.assertEqual(allocation.allocate(transport, state, 'G4', math.inf), 'next-window-vm')
            first, unclear, confirmed, replacement = transport.calls
            self.assertEqual(first[2], unclear[2])
            self.assertEqual(first[2], confirmed[2])
            self.assertNotEqual(first[2], replacement[2])
            self.assertTrue(all(math.isfinite(call[3]) for call in transport.calls))

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
