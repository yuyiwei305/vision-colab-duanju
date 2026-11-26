"""Small native-Comfy boundary for explicit Pika attention and measured six-NFE runs.

No model, sampler or attention kernel is reimplemented here. Requests are serial;
kernel hooks count the actual native calls and never change their arguments.
"""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

import torch
import comfy_kitchen
from comfy.ldm.modules import attention
from server import PromptServer
from aiohttp import web

BACKENDS = ('pytorch', 'comfy_kitchen_int8', 'sage3')
RUNS = {}
ORIGINAL_CK = comfy_kitchen.int8_attention
ORIGINAL_CK_PRE = comfy_kitchen.int8_attention_from_prequantized
ORIGINAL_SAGE3 = getattr(attention, 'sageattn3_blackwell', None)
PREFLIGHT = None
PREFLIGHT_LOCK = asyncio.Lock()


def save_metrics(request_id):
    request_id = str(uuid.UUID(request_id))
    directory = Path(os.environ['H3_METRICS_DIR'])
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, prefix='.metrics-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(RUNS[request_id], stream)
        os.replace(temporary, directory / (request_id + '.json'))
    finally:
        Path(temporary).unlink(missing_ok=True)


def preflight():
    results = {}
    device = torch.cuda.current_device()
    if torch.cuda.get_device_capability(device)[0] != 12:
        raise RuntimeError('expected_blackwell_sm120')
    with torch.inference_mode():
        generator = torch.Generator(device='cuda').manual_seed(42)
        q, k, v = [torch.randn(1, 56, 2048, 128, device='cuda', dtype=torch.bfloat16,
                               generator=generator) for _ in range(3)]
        reference = attention.get_attention_function('pytorch')(q, k, v, 56, skip_reshape=True)
        for backend in BACKENDS:
            function = attention.get_attention_function(backend, None)
            if function is None: raise RuntimeError('attention_backend_unavailable_' + backend)
            torch.cuda.synchronize()
            started = time.perf_counter()
            sage_calls = []
            if backend == 'sage3':
                def checked_sage(*args, **kwargs):
                    output = ORIGINAL_SAGE3(*args, **kwargs)
                    sage_calls.append(True)
                    return output
                attention.sageattn3_blackwell = checked_sage
            try:
                output = function(q, k, v, 56, skip_reshape=True)
            finally:
                if ORIGINAL_SAGE3 is not None: attention.sageattn3_blackwell = ORIGINAL_SAGE3
            torch.cuda.synchronize()
            if not bool(torch.isfinite(output).all()):
                raise RuntimeError('nonfinite_attention_' + backend)
            if backend == 'sage3' and not sage_calls:
                raise RuntimeError('sage3_preflight_fell_back')
            results[backend] = {'finite': True, 'seconds': time.perf_counter()-started,
                'relative_l2_to_pytorch': float((output.float()-reference.float()).norm()/reference.float().norm())}
    return {'state': 'ready', 'gpu': torch.cuda.get_device_name(device),
            'capability': list(torch.cuda.get_device_capability(device)), 'backends': results}


@PromptServer.instance.routes.get('/h3/pika-preflight')
async def preflight_route(request):
    global PREFLIGHT
    async with PREFLIGHT_LOCK:
        if PREFLIGHT is None:
            try: PREFLIGHT = await asyncio.to_thread(preflight)
            except Exception as exc: PREFLIGHT = {'state': 'failed', 'error_type': type(exc).__name__,
                                                  'reason': str(exc)[:300]}
    return web.json_response(PREFLIGHT)


class H3PikaAttention:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'model': ('MODEL',), 'backend': (list(BACKENDS),),
                             'request_id': ('STRING', {'default': ''})}}
    RETURN_TYPES = ('MODEL',)
    FUNCTION = 'apply'
    CATEGORY = 'Vision Media/H3'

    def apply(self, model, backend, request_id):
        request_id = str(uuid.UUID(request_id))
        function = attention.get_attention_function(backend, None)
        if function is None: raise RuntimeError('required_attention_backend_unavailable')
        stats = {'backend': backend, 'nfe': 0, 'attention_dispatches': 0,
                 'ck_int8_calls': 0, 'ck_prequantized_calls': 0, 'sage3_kernel_calls': 0,
                 'model_seconds': [], 'kernel_errors': [], 'started_at': time.time(), 'finished': False}
        RUNS[request_id] = stats
        comfy_kitchen.int8_attention = ORIGINAL_CK
        comfy_kitchen.int8_attention_from_prequantized = ORIGINAL_CK_PRE
        if ORIGINAL_SAGE3 is not None: attention.sageattn3_blackwell = ORIGINAL_SAGE3

        def count_kernel(original, key):
            def invoke(*args, **kwargs):
                try:
                    output = original(*args, **kwargs)
                except Exception as exc:
                    stats['kernel_errors'].append({'kernel': key, 'type': type(exc).__name__})
                    raise
                stats[key] += 1
                return output
            return invoke
        if backend == 'comfy_kitchen_int8':
            comfy_kitchen.int8_attention = count_kernel(ORIGINAL_CK, 'ck_int8_calls')
            comfy_kitchen.int8_attention_from_prequantized = count_kernel(ORIGINAL_CK_PRE, 'ck_prequantized_calls')
        if backend == 'sage3':
            if ORIGINAL_SAGE3 is None: raise RuntimeError('sage3_not_installed')
            attention.sageattn3_blackwell = count_kernel(ORIGINAL_SAGE3, 'sage3_kernel_calls')

        def tracked(*args, **kwargs):
            stats['attention_dispatches'] += 1
            return function(*args, **kwargs)
        container = getattr(function, 'container_function', None)
        if container is not None:
            def tracked_container(*args, **kwargs):
                stats['attention_dispatches'] += 1
                return container(*args, **kwargs)
            tracked.container_function = tracked_container

        torch.cuda.reset_peak_memory_stats()
        result = model.clone()
        result.set_model_optimized_attention(tracked)
        def measured_model(model_function, arguments):
            torch.cuda.synchronize()
            started = time.perf_counter()
            stats['nfe'] += 1
            try:
                return model_function(arguments['input'], arguments['timestep'], **arguments['c'])
            finally:
                torch.cuda.synchronize()
                stats['model_seconds'].append(time.perf_counter()-started)
                stats['peak_allocated_bytes'] = torch.cuda.max_memory_allocated()
                stats['peak_reserved_bytes'] = torch.cuda.max_memory_reserved()
                save_metrics(request_id)
        result.set_model_unet_function_wrapper(measured_model)
        return (result,)


class H3PikaFinalize:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'video': ('VIDEO',), 'request_id': ('STRING', {'default': ''})}}
    RETURN_TYPES = ('VIDEO',)
    FUNCTION = 'finish'
    CATEGORY = 'Vision Media/H3'

    def finish(self, video, request_id):
        stats = RUNS[str(uuid.UUID(request_id))]
        torch.cuda.synchronize()
        stats.update(finished=True, finished_at=time.time(),
                     peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                     peak_reserved_bytes=torch.cuda.max_memory_reserved(),
                     peak_scope='attention_node_through_decode')
        if stats['nfe'] != 6: raise RuntimeError('pika_requires_exactly_six_nfe')
        if stats['kernel_errors']: raise RuntimeError('required_attention_kernel_failed')
        if stats['backend'] == 'sage3' and stats['sage3_kernel_calls'] == 0:
            raise RuntimeError('sage3_kernel_not_used')
        if stats['backend'] == 'comfy_kitchen_int8' and stats['ck_int8_calls'] + stats['ck_prequantized_calls'] == 0:
            raise RuntimeError('comfy_kitchen_kernel_not_used')
        save_metrics(request_id)
        return (video,)


NODE_CLASS_MAPPINGS = {'H3PikaAttention': H3PikaAttention, 'H3PikaFinalize': H3PikaFinalize}
