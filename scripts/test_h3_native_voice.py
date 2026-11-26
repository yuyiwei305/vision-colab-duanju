"""Real extraction/integrity and the 90-second continuation contract; no GPU."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

import h3_native_voice as voice
import h3_workflow_v4 as v4


class VoiceTests(unittest.TestCase):
    def test_future_or_out_of_range_voice_dependencies_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'must_precede'):
            voice.parse('- 原生音色参考：MOTION-EP001-001/LOCKARD/0.6/3.0', {})
        with self.assertRaisesRegex(ValueError, 'window'):
            voice.parse('- 原生音色参考：MOTION-EP001-001/LOCKARD/0.6/99',
                        {'MOTION-EP001-001': 'EP001-001-test'})

    def test_real_native_excerpt_is_reused_and_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); cache = root / 'receipts'; cache.mkdir()
            source = root / 'native.mp4'
            subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i',
                'color=black:s=16x16:r=24', '-f', 'lavfi', '-i', 'sine=frequency=220:sample_rate=32000',
                '-t', '4', '-c:v', 'libx264', '-c:a', 'aac', str(source)], check=True)
            (cache / 'source.json').write_text(json.dumps({'job_id': 'EP001-001-test',
                'files': [{'target': 'native.mp4', 'sha256': voice.sha(source)}]}))
            recipes = voice.parse('- 原生音色参考：MOTION-EP001-001/LOCKARD/0.6/3.0',
                                  {'MOTION-EP001-001': 'EP001-001-test'})
            first = voice.resolve(root, cache, recipes)
            second = voice.resolve(root, cache, recipes)
            self.assertEqual(first, second)
            self.assertEqual(first[0]['duration_seconds'], 3)
            self.assertEqual(first[0]['method'], 'native_excerpt_no_tts_no_separation')
            (root / first[0]['path']).write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'integrity_mismatch'):
                voice.resolve(root, cache, recipes)


class ContinuationTests(unittest.TestCase):
    def test_complete_sixteen_segment_seven_chain_film_is_exactly_2160_frames(self):
        heads = {1, 4, 6, 8, 10, 12, 14}
        five = {6, 10, 14}
        parameters = [v4.parameters('drama', i, 5 if i in five else 6,
                                   parent=None if i in heads else 'previous', context_preset='22v18a')
                      for i in range(1, 17)]
        self.assertEqual(sum(p['frame_count'] - p['context_frames'] for p in parameters), 2160)
        self.assertEqual(parameters[1]['frame_count'], 158)
        self.assertEqual(parameters[1]['context_audio_frames'], 18)
        self.assertAlmostEqual(parameters[1]['dialogue_offset_seconds'], 22/24)

    def test_voice_refs_and_timeline_audio_coexist_in_continuation_graph(self):
        p = v4.parameters('drama', 1, 6, parent='previous', context_preset='22v18a')
        p['native_voice_references'] = [{'speaker': 'LOCKARD'}, {'speaker': 'ELLIOTT'}]
        job = {'modality': 'video', 'outputs': ['native.mp4', 'master.mp4'],
               'parameters': p, 'prompt': 'summary: Talk.\ndetailed_description: Speak.',
               'reference_bindings': [{'role': 'reference_image', 'label': 'identity',
                                      'may_control': ['identity'], 'must_not_control': ['dialogue']}]}
        graph = v4.build_graph(job, ['a'*64+'.png'], 'preflight',
            {'path': 'preflight-context.safetensors', 'voice_filenames': ['b'*64+'.wav', 'c'*64+'.wav']})
        self.assertEqual(graph['41']['inputs']['audio_count'], 2)
        self.assertEqual(graph['92']['inputs']['context_length'], '22')
        self.assertEqual(graph['92']['inputs']['audio_context_length'], 18)
        self.assertEqual(graph['11']['inputs']['positive'], ['92', 0])
        self.assertEqual(graph['17']['inputs']['audio'], ['93', 1])


if __name__ == '__main__':
    unittest.main()
