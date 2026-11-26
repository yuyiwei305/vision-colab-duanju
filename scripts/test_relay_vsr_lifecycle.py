"""Regression tests for losing the sole remote output during failure cleanup."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import relay_vsr_colab as r

class FakeColab:
    actions=[]
    def __init__(self,*args):pass
    def control(self,action,value=''):
        self.actions.append(action)
        if action=='assignments':return [] if 'unassign' in self.actions else [{'endpoint':'test-only'}]
        if action=='usage':return {'rate':0,'count':0,'balance':100}
        if action=='session':return None
        return {}

class ReleaseBoundary(unittest.TestCase):
    target=r
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name).resolve();self.state=self.root/'state.json'
        self.batch={'session':'test','state_file':str(self.state)}
        FakeColab.actions=[]
    def test_refuses_unassign_when_only_remote_copy_exists(self):
        r.h.write_json(self.state,{'state':'running','endpoint':'test-only','worker_started':True,'active_artifacts_preserved':False})
        with patch.object(r.h,'Colab',FakeColab):
            with self.assertRaisesRegex(RuntimeError,'release_blocked'):
                self.target.release(self.batch)
        self.assertNotIn('unassign',FakeColab.actions)
    def test_detects_local_corruption_at_release_boundary(self):
        saved=self.root/'saved.mp4';saved.write_bytes(b'original');digest=r.h.digest(saved)
        r.h.write_json(self.state,{'state':'ready','endpoint':'test-only','worker_started':True,'active_artifacts_preserved':True,
            'preserved':{'final.mp4':{'local':str(saved),'sha256':digest}}})
        saved.write_bytes(b'corrupted')
        with patch.object(r.h,'Colab',FakeColab):
            with self.assertRaisesRegex(RuntimeError,'preserved_file_changed'):self.target.release(self.batch)
        self.assertNotIn('unassign',FakeColab.actions)
    def test_allows_verified_copy_and_checks_assignment_disappeared(self):
        saved=self.root/'saved.mp4';saved.write_bytes(b'original')
        r.h.write_json(self.state,{'state':'ready','endpoint':'test-only','worker_started':True,'active_artifacts_preserved':True,
            'preserved':{'final.mp4':{'local':str(saved),'sha256':r.h.digest(saved)}}})
        with patch.object(r.h,'Colab',FakeColab):result=self.target.release(self.batch)
        self.assertEqual(result['state'],'released');self.assertIn('unassign',FakeColab.actions)
        self.assertEqual(result['assignments_after'],[])

if __name__=='__main__':unittest.main()
