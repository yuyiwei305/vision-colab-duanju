"""Measurement only: the pinned upstream nodes own conditioning and sampling."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import time
import uuid

from aiohttp import web
import torch
import comfy_kitchen as kitchen
from comfy.ldm.modules import attention
from server import PromptServer

RUNS = {}
KERNELS = ('int8_attention', 'int8_attention_from_prequantized', 'sol_attn', 'sol_attn_chunked')
ORIGINAL = {name: getattr(kitchen, name, None) for name in KERNELS}
PREFLIGHT = None
PREFLIGHT_LOCK = asyncio.Lock()


def save(request_id):
    directory = Path(os.environ['H3_METRICS_DIR'])
    directory.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.v4-', dir=directory)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(RUNS[request_id], stream)
        os.replace(temporary, directory / (request_id + '.json'))
    finally:
        Path(temporary).unlink(missing_ok=True)


def preflight():
    if torch.cuda.get_device_capability()[0] != 12:
        raise RuntimeError('expected_blackwell_sm120')
    if any(ORIGINAL[name] is None for name in KERNELS):
        raise RuntimeError('required_kitchen_kernel_missing')
    function = attention.get_attention_function('comfy_kitchen_int8', None)
    if function is None: raise RuntimeError('required_kitchen_attention_missing')
    with torch.inference_mode():
        generator = torch.Generator(device='cuda').manual_seed(42)
        q, k, v = [torch.randn(1, 56, 2048, 128, device='cuda', dtype=torch.bfloat16,
                              generator=generator) for _ in range(3)]
        dense = function(q, k, v, 56, skip_reshape=True)
        sparse = kitchen.sol_attn(q.transpose(1, 2), k.transpose(1, 2), v.transpose(1, 2), tau=1.3)
        if not bool(torch.isfinite(dense).all()) or not bool(torch.isfinite(sparse).all()):
            raise RuntimeError('nonfinite_attention_preflight')
        torch.cuda.synchronize()
    return {'state': 'ready', 'gpu': torch.cuda.get_device_name(),
            'dense_and_sol_finite': True, 'inference_validated': False}


@PromptServer.instance.routes.get('/h3/v4-preflight')
async def preflight_route(request):
    global PREFLIGHT
    async with PREFLIGHT_LOCK:
        if PREFLIGHT is None:
            try:
                PREFLIGHT = await asyncio.to_thread(preflight)
            except Exception as exc:
                PREFLIGHT = {'state': 'failed', 'error_type': type(exc).__name__, 'reason': str(exc)[:300]}
    return web.json_response(PREFLIGHT)


class H3V4Measure:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'model': ('MODEL',), 'request_id': ('STRING',),
                             'expected_nfe': ('INT', {'min': 1, 'max': 30}),
                             'scene_profile': (['drama', 'action'],)}}
    RETURN_TYPES = ('MODEL',)
    FUNCTION = 'apply'
    CATEGORY = 'Vision Media/H3 V4'

    def apply(self, model, request_id, expected_nfe, scene_profile):
        request_id = str(uuid.UUID(request_id))
        stats = {'scene_profile': scene_profile, 'expected_nfe': expected_nfe, 'nfe': 0,
                 'kernel_calls': {name: 0 for name in KERNELS}, 'model_seconds': [],
                 'model_timesteps': [], 'finished': False, 'started_at': time.time(),
                 'highres_tiling': False, 'temporal_chunking': False}
        RUNS[request_id] = stats
        for name, original in ORIGINAL.items():
            if original is None: raise RuntimeError('required_kitchen_kernel_missing')
            def tracked(*args, _function=original, _name=name, **kwargs):
                value = _function(*args, **kwargs)
                stats['kernel_calls'][_name] += 1
                return value
            setattr(kitchen, name, tracked)
        result = model.clone()
        previous = model.model_options.get('model_function_wrapper')
        torch.cuda.reset_peak_memory_stats()
        def measured(function, arguments):
            torch.cuda.synchronize()
            started = time.perf_counter()
            stats['nfe'] += 1
            stats['model_timesteps'].append(float(arguments['timestep'].max().detach().cpu()))
            try:
                if previous: return previous(function, arguments)
                return function(arguments['input'], arguments['timestep'], **arguments['c'])
            finally:
                torch.cuda.synchronize()
                stats['model_seconds'].append(time.perf_counter() - started)
                stats['peak_allocated_bytes'] = torch.cuda.max_memory_allocated()
                stats['peak_reserved_bytes'] = torch.cuda.max_memory_reserved()
                save(request_id)
        result.set_model_unet_function_wrapper(measured)
        return (result,)


class H3V4Sigmas:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'sigmas': ('SIGMAS',), 'request_id': ('STRING',)}}
    RETURN_TYPES = ('SIGMAS',)
    FUNCTION = 'record'
    CATEGORY = 'Vision Media/H3 V4'

    def record(self, sigmas, request_id):
        request_id = str(uuid.UUID(request_id))
        stats = RUNS[request_id]
        if len(sigmas) != stats['expected_nfe'] + 1:
            raise RuntimeError('v4_unexpected_sigma_schedule')
        stats['sigmas'] = sigmas.detach().cpu().tolist()
        save(request_id)
        return (sigmas,)


class H3V4Finalize:
    @classmethod
    def INPUT_TYPES(cls):
        return {'required': {'images': ('IMAGE',), 'audio': ('AUDIO',), 'request_id': ('STRING',)}}
    RETURN_TYPES = ('IMAGE', 'AUDIO')
    FUNCTION = 'finish'
    CATEGORY = 'Vision Media/H3 V4'

    def finish(self, images, audio, request_id):
        request_id = str(uuid.UUID(request_id))
        stats = RUNS[request_id]
        torch.cuda.synchronize()
        stats.update(finished=True, finished_at=time.time(),
                     peak_allocated_bytes=torch.cuda.max_memory_allocated(),
                     peak_reserved_bytes=torch.cuda.max_memory_reserved(),
                     peak_scope='model_measure_through_audio_video_decode')
        try:
            if stats['nfe'] != stats['expected_nfe']:
                raise RuntimeError('v4_unexpected_nfe')
            if not stats['kernel_calls']['int8_attention'] + stats['kernel_calls']['int8_attention_from_prequantized']:
                raise RuntimeError('v4_kitchen_kernel_not_used')
            save(request_id)
        finally:
            for name, function in ORIGINAL.items(): setattr(kitchen, name, function)
        return (images, audio)


NODE_CLASS_MAPPINGS = {'H3V4Measure': H3V4Measure, 'H3V4Sigmas': H3V4Sigmas, 'H3V4Finalize': H3V4Finalize}
