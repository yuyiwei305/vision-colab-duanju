"""Versioned V4 inference profile; resource ownership stays in colab_h3.

Routing is local and inspectable, performed once before prepare. No inference,
network access, or hidden LLM calls occur while choosing a scene profile.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

PROFILE = 'h3-v4-g4-fullframe-v1'
RUNTIME = 'docs/h3/workflow-v4-g4-runtime.json'
RUNTIME_PATH = Path(__file__).resolve().parents[1] / RUNTIME
MODEL = 'Cobijada_H3_Hybrid_Uncensored_pruned_hybrid_int8_convrot_pruned.safetensors'
CLIP = 'qwen3vl_32b_minimax_h3_int8_convrot.safetensors'
UPSCALER = 'minimax_h3_latent_upscaler_3d_fp16_V2.safetensors'
COMMON_LORA = ('minimax_h3_fl2v_lightx2v_turbo_4step_v0.1_comfy.safetensors', 0.7)
SCENES = {
    'drama': {'steps': 8, 'transition_step': 6, 'loras': [
        ('h3-realism-people-t2v-i2v-r2v.safetensors', 0.8),
        ('Minimax H3真实电影质感V0.1.safetensors', 0.3)]},
    'action': {'steps': 12, 'transition_step': 8, 'loras': [
        ('H3_Combat_V2.safetensors', 0.8),
        ('FrameRush-Minimax-V2_c2-st1500.safetensors', 0.5),
        ('H3_speed_slider_1.1.safetensors', 5.0)]},
}
ACTION = re.compile(
    r'\b(?:fight(?:s|ing)?|combat|brawl(?:s|ing)?|grappl\w*|punch(?:es|ing)?|'
    r'kick(?:s|ing)?|parr(?:y|ies|ying)|slash(?:es|ing)?|stab(?:s|bing)?|'
    r'lunge(?:s)?|lunging|chase(?:s)?|chasing|sprint(?:s|ing)?|gunfight|'
    r'swordfight|clash(?:es|ing)?)\b|'
    r'打斗|交锋|搏斗|格斗|扭打|对砍|劈砍|挥砍|刺向|踢向|重拳|枪战|追逐|追赶|冲刺', re.I)


def select_scene(section, prompt):
    """Explicit source override wins; quoted dialogue and negative clauses don't route action."""
    override = re.search(r'^- H3档位：(文戏|打斗|自动)\s*$', section, re.M)
    if re.search(r'^- H3档位：', section, re.M) and not override:
        raise ValueError('invalid_scene_profile_override')
    if override and override[1] != '自动':
        return {'scene_profile': {'文戏': 'drama', '打斗': 'action'}[override[1]],
                'explicit': True, 'reason': 'source_override'}
    # Identity/reference descriptions and soundscape are not performed actions.
    parts = re.findall(r'(?ims)^(?:summary|detailed_description):\s*(.*?)(?=^\w+:|\Z)', prompt)
    text = '\n'.join(parts)
    text = re.sub(r'"[^"\n]*"|“[^”\n]*”|「[^」\n]*」', ' ', text)
    text = re.sub(r'(?i)\b(?:no|not|without|never|avoid|do not)\b[^.!?;\n]*', ' ', text)
    text = re.sub(r'(?:没有|不要|禁止|不再|无须|无需)[^。！？；\n]*', ' ', text)
    text = re.sub(r'(?i)\b(?:talks?|speaks?|talking|speaking)\s+about[^.!?;\n]*', ' ', text)
    matches = sorted({m.group(0).lower() for m in ACTION.finditer(text)})
    return {'scene_profile': 'action' if matches else 'drama', 'explicit': False,
            'reason': 'action_cues:' + ','.join(matches) if matches else 'no_performed_action_cue'}


def route_chains(records):
    """One compatible LoRA stack per complete, adjacent AV continuation chain."""
    groups = []
    for entry, parent, route in records:
        if parent == '无': groups.append([])
        elif not groups or not groups[-1] or parent != groups[-1][-1][0]:
            raise ValueError('context_must_reference_previous_entry')
        groups[-1].append((entry, route))
    result = {}
    for group in groups:
        explicit = {r['scene_profile'] for _, r in group if r['explicit']}
        chosen = 'action' if any(r['scene_profile'] == 'action' for _, r in group) else 'drama'
        if len(explicit) > 1 or ('drama' in explicit and chosen == 'action'):
            raise ValueError('continuation_scene_profile_conflict')
        for entry, route in group:
            result[entry] = {**route, 'scene_profile': chosen,
                             'routing_policy': 'local-action-cues-v1-chain-consistent',
                             'chain_head': group[0][0]}
    return result


def parameters(scene, seed, duration, ratio='16:9', parent=None, context_preset='5v24a'):
    if scene not in SCENES or type(seed) is not int or not 0 <= seed < 2**63:
        raise ValueError('invalid_v4_scene_or_seed')
    if type(duration) is not int or not 4 <= duration <= 15 or ratio not in ('16:9', '9:16'):
        raise ValueError('invalid_v4_duration_or_canvas')
    width, height = (1344, 768) if ratio == '16:9' else (768, 1344)
    if context_preset not in ('5v24a', '22v18a'):
        raise ValueError('unsupported_v4_context_preset')
    visual, audio = (22, 18) if context_preset == '22v18a' else (5, 24)
    frames = max(0, round((duration * 24 - 5) / 17)) * 17 + 5
    if parent and context_preset == '22v18a':
        frames += 17
    spec = SCENES[scene]
    return {'profile': PROFILE, 'model': 'cobijada-h3-hybrid-int8-convrot',
            'prompt_language': 'en', 'scene_profile': scene, 'seed': seed,
            'ratio': ratio, 'width': width, 'height': height,
            'internal_width': width, 'internal_height': height,
            'duration': duration, 'frame_rate': 24,
            'frame_count': frames, 'context_preset': context_preset,
            'context_audio_frames': audio if parent else 0,
            'dialogue_offset_seconds': visual / 24 if parent else 0,
            'steps': spec['steps'], 'expected_nfe': spec['steps'] + 1,
            'transition_step': spec['transition_step'], 'sampler': 'euler',
            'scheduler': 'simple', 'denoise': 1.0, 'cfg': 1.0,
            'sigma_refiner': {'extra_steps': 1, 'start_at_sigma': 0.7,
                              'end_at_sigma': 0.0, 'spacing': 'cosine'},
            'loras': [{'name': name, 'strength': weight} for name, weight in [COMMON_LORA, *spec['loras']]],
            'lowres_scale': 0.5, 'rho': 0.0, 'w_min': 0.5, 'w_max': 1.0,
            'highres_tiling': False, 'temporal_chunking': False,
            'attention_backend': 'comfy_kitchen_int8', 'sparse_attention': 'sol-attn',
            'reference_image_size': '2k', 'crf': 12,
            'continue_from_job': parent, 'context_frames': visual if parent else 0}


def reference_capabilities():
    return json.loads(RUNTIME_PATH.read_text(encoding='utf-8'))['capabilities']


def reference_counts_fit(capabilities, images, audios):
    limits = capabilities['reference_image_count']
    return (limits['min'] <= images <= limits['max']
            and audios <= capabilities['max_reference_audio_count']
            and images + audios <= capabilities['max_total_references'])


def build_graph(job, filenames, request_id, context=None):
    p = job['parameters']
    fixed = parameters(p.get('scene_profile'), p.get('seed'), p.get('duration'),
                       p.get('ratio'), p.get('continue_from_job'), p.get('context_preset', '5v24a'))
    if any(p.get(k) != v for k, v in fixed.items()):
        raise ValueError('unsupported_v4_parameters')
    bindings = job.get('reference_bindings', [])
    images = [b for b in bindings if b['role'] == 'reference_image']
    audios = [b for b in bindings if b['role'] == 'reference_audio']
    capabilities = reference_capabilities()
    expected_outputs = 2 if p['continue_from_job'] else 1
    if (job.get('modality') != 'video' or len(job.get('outputs', [])) != expected_outputs
            or not reference_counts_fit(capabilities, len(images), len(audios))
            or len(images) + len(audios) != len(bindings) or len(filenames) != len(bindings)
            or not re.fullmatch(r'[A-Za-z0-9-]{1,100}', request_id)):
        raise ValueError('unsupported_v4_job_or_references')
    voice_recipes = p.get('native_voice_references', [])
    if not reference_counts_fit(capabilities, len(images), len(audios) + len(voice_recipes)):
        raise ValueError('too_many_native_voice_references')
    contract = ' '.join(f'<Picture {i}>: label={b["label"]}; allowed={b["may_control"]}; '
                        f'not controlled={b["must_not_control"]}.' for i, b in enumerate(images, 1))
    prompt = job['prompt']
    if '\ndetailed_description:' not in prompt:
        raise ValueError('h3_full_reference_prompt_required')
    prompt = prompt.replace('\ndetailed_description:', '\n' + contract + '\ndetailed_description:', 1)
    if p['scene_profile'] == 'drama' and not re.search(r'\br34l1sm\b', prompt):
        prompt = 'r34l1sm\n' + prompt
    def n(kind, **inputs): return {'class_type': kind, 'inputs': inputs}
    graph = {
        '1': n('UNETLoader', unet_name=MODEL, weight_dtype='default'),
        '2': n('CLIPLoader', clip_name=CLIP, type='minimax', device='default'),
        '3': n('VAELoader', vae_name='minimax_h3_video_vae_int8_convrot.safetensors'),
        '4': n('VAELoader', vae_name='minimax_h3_audio_vae_fp32.safetensors'),
        '5': n('MiniMaxH3EasyModelAdapter', text_encoder=['2', 0], video_vae=['3', 0],
               audio_vae=['4', 0], fl2va_model=['1', 0], ref2va_model=['1', 0]),
        '6': n('MiniMaxH3Easy', h3_bundle=['5', 0], mode='reference', prompt=prompt,
               resolution='custom', aspect_ratio=p['ratio'], width=p['width'], height=p['height'],
               seconds=(p['frame_count'] / 24 if p.get('context_preset') == '22v18a'
                        else p['duration']), advanced=True, fps=24.0, keyframe_role='first',
               ref_image_size='2k', reference_mention_mode='index',
               prompt_optimizer_settings=False, prompt_optimizer_scene_guide='none', media=['41', 0]),
        '7': n('MiniMaxH3EasyOutput', h3_context=['6', 1]),
        '8': n('KSamplerSelect', sampler_name='euler'),
        '9': n('BasicScheduler', model=['31', 0], scheduler='simple', steps=p['steps'], denoise=1.0),
        '10': n('H3SigmaRefiner', sigmas=['9', 0], **p['sigma_refiner']),
        '11': n('SelfLiftAvatarH3Sampler', low_res_model=['31', 0], positive=['7', 0],
                negative=['42', 0], vae=['3', 0], latent_image=['7', 1], sampler=['8', 0],
                sigmas=['43', 0], seed=p['seed'], cfg=1.0, transition_step=p['transition_step'],
                lowres_scale=0.5, rho=0.0, w_min=0.5, w_max=1.0, upscaler_model=UPSCALER,
                highres_tiling=False),
        '12': n('LTXVSeparateAVLatent', av_latent=['11', 0]),
        '13': n('VAEDecode', samples=['12', 0], vae=['3', 0]),
        '14': n('VAEDecodeAudio', samples=['12', 1], vae=['4', 0]),
        '16': n('H3V4Finalize', images=['13', 0], audio=['14', 0], request_id=request_id),
        '31': n('H3V4Measure', model=['33', 0], request_id=request_id,
                expected_nfe=p['expected_nfe'], scene_profile=p['scene_profile']),
        '32': n('ModelAttentionBackend', model=['52', 0], attention='comfy kitchen attention'),
        '33': n('BlockSparseAttention', **{'model': ['32', 0], 'selection': 'sol-attn',
                'selection.tau': 1.3, 'start_percent': 0.2, 'end_percent': 0.9, 'dense_blocks': '',
                'min_tokens': 12288, 'extra_tokens': 256, 'sink_conditioning': 'exact_kv_and_rows',
                'verbose': False}),
        '41': n('MiniMaxH3EasyMediaBridge', image_count=len(images), audio_count=len(audios), video_count=0),
        '42': n('ConditioningZeroOut', conditioning=['7', 0]),
        '43': n('H3V4Sigmas', sigmas=['10', 0], request_id=request_id),
        '90': n('MiniMaxH3MotionContextSaveLatent', latent=['11', 0],
                filename_prefix='context-' + request_id, clip_index=1),
    }
    model = ['6', 0]
    for i, lora in enumerate(p['loras']):
        node = str(50 + i)
        graph[node] = n('LoraLoaderModelOnly', model=model, lora_name=lora['name'], strength_model=lora['strength'])
        model = [node, 0]
    graph['32']['inputs']['model'] = model
    image_index = audio_index = 0
    for i, (binding, filename) in enumerate(zip(bindings, filenames)):
        audio = binding['role'] == 'reference_audio'
        pattern = r'[0-9a-f]{64}\.(wav|mp3|flac|m4a|aac)' if audio else r'[0-9a-f]{64}\.(png|jpg|jpeg|webp)'
        if not re.fullmatch(pattern, filename): raise ValueError('unsafe_reference_filename')
        node = str(200 + i)
        graph[node] = n('LoadAudio', audio=filename) if audio else n('LoadImage', image=filename)
        if audio:
            audio_index += 1; graph['41']['inputs'][f'audio_{audio_index}'] = [node, 0]
        else:
            image_index += 1; graph['41']['inputs'][f'image_{image_index}'] = [node, 0]
    if voice_recipes:
        voices = (context or {}).get('voice_filenames')
        if voices is None:
            # Preflight validates graph structure before the parent media exists.
            voices = ['0' * 64 + '.wav'] * len(voice_recipes)
        if len(voices) != len(voice_recipes):
            raise ValueError('native_voice_reference_count_mismatch')
        for i, filename in enumerate(voices):
            if not re.fullmatch(r'[0-9a-f]{64}\.wav', filename):
                raise ValueError('unsafe_native_voice_filename')
            node = str(70 + i)
            graph[node] = n('LoadAudio', audio=filename)
            graph['41']['inputs'][f'audio_{len(audios) + i + 1}'] = [node, 0]
        graph['41']['inputs']['audio_count'] = len(audios) + len(voice_recipes)
    native_images, native_audio = ['16', 0], ['16', 1]
    if p['continue_from_job']:
        path = (context or {'path': 'preflight-context.safetensors'})['path']
        if not re.fullmatch(r'(?:context-[a-f0-9-]+_00001|preflight-context)\.safetensors', path):
            raise ValueError('invalid_context_path')
        graph['91'] = n('MiniMaxH3MotionContextLoadLatent', latent_path=path, clip_index=1)
        graph['92'] = n('MiniMaxH3MotionContext', conditioning=['7', 0], vae=['3', 0],
                        latent=['7', 1], context_latent=['91', 0],
                        context_length=str(p['context_frames']),
                        audio_context_length=p['context_audio_frames'])
        graph['11']['inputs']['positive'] = ['92', 0]
        graph['42']['inputs']['conditioning'] = ['92', 0]
        graph['93'] = n('MiniMaxH3MotionContextTrim', images=['16', 0], audio=['16', 1],
                        trim_frames=['92', 1], fps=24.0, match_tail=True)
        native_images, native_audio = ['93', 0], ['93', 1]
    outputs = [('17', 'h3-', native_images, native_audio)]
    if p['continue_from_job']: outputs.append(('19', 'master-', ['16', 0], ['16', 1]))
    for node, prefix, frames, audio in outputs:
        graph[node] = n('VHS_VideoCombine', images=frames, audio=audio, frame_rate=24.0,
                        loop_count=0, filename_prefix=prefix + request_id, format='video/h264-mp4',
                        pix_fmt='yuv420p', crf=12, save_metadata=True, trim_to_audio=False,
                        pingpong=False, save_output=True)
    return graph
