import importlib.util
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec=importlib.util.spec_from_file_location('drama_edit_safe',Path(__file__).with_name('drama-edit-safe.py'))
safe=importlib.util.module_from_spec(spec);spec.loader.exec_module(safe)

class RenderGuardTests(unittest.TestCase):
    def test_existing_output_and_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);ep=root/'EP';out=ep/'制作成果/成片';out.mkdir(parents=True)
            safe.inspect_output(ep)
            (out/'成片.mp4').write_bytes(b'user-data')
            with self.assertRaises(safe.SafetyError): safe.inspect_output(ep)
            self.assertEqual((out/'成片.mp4').read_bytes(),b'user-data')
            (out/'成片.mp4').unlink();out.rmdir()
            (root/'elsewhere').mkdir();out.symlink_to(root/'elsewhere',target_is_directory=True)
            with self.assertRaises(safe.SafetyError): safe.inspect_output(ep)

    def test_charset_check_covers_all_required_cjk(self):
        self.assertTrue(safe.covers_charset('20-7f 4e00-9fff'))
        self.assertFalse(safe.covers_charset('20-7f'))
        self.assertFalse(safe.covers_charset('not-a-codepoint'))

    def test_missing_chinese_font_fails_closed(self):
        with tempfile.TemporaryDirectory() as name, patch.object(safe,'match_font',return_value=('Latin','/tmp/font','20-7f')):
            with self.assertRaises(safe.SafetyError):safe.font_selection(dict(os.environ),Path(name))

    @unittest.skipUnless(Path('/opt/homebrew/etc/fonts/fonts.conf').is_file(), 'requires local Fontconfig setup')
    def test_rendering_two_hanzi_produces_distinct_glyphs(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name);env,family,fontdir=safe.font_selection(dict(os.environ),root)
            base=('''[Script Info]\nScriptType: v4.00+\nPlayResX: 320\nPlayResY: 180\n'''
                  '''[V4+ Styles]\nFormat: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding\n'''
                  f'''Style: Default,{family},70,&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,5,0,0,0,1\n'''
                  '''[Events]\nFormat: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text\n''')
            hashes=[]
            for char in ('你','好'):
                ass=root/f'{ord(char)}.ass';ass.write_text(base+f'Dialogue: 0,0:00:00.00,0:00:01.00,Default,,0,0,0,,{char}\n',encoding='utf8')
                result=subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','color=black:s=320x180:d=1',
                   '-vf',safe.with_fontsdir(f"ass='{ass}'",fontdir),'-frames:v','1','-f','framemd5','-'],env=env,capture_output=True,text=True,check=True)
                hashes.append(next(line for line in result.stdout.splitlines() if not line.startswith('#')).split(',')[-1].strip())
            self.assertNotEqual(*hashes,'different characters must not render the same tofu glyph')

if __name__=='__main__':unittest.main()
