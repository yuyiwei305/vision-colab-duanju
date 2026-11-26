#!/usr/bin/env python3
"""Non-destructive launcher for the project's pinned short-drama editor.

Existing outputs must be rendered to a
fresh episode staging directory; a verified local CJK font is passed to libass
for this invocation only and never changes the system configuration.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

PROJECT = Path(__file__).resolve().parent.parent
EDITOR_REL = 'skills/short-drama-edit/scripts/edit_tool.py'
REQUIRED_GLYPHS = (ord('中'), ord('字'), ord('我'), ord('剧'))

class SafetyError(Exception):
    pass

def pinned_editor(project: Path) -> Path:
    lock = json.loads((project / 'drama-skills.lock.json').read_text(encoding='utf8'))
    editor = project / 'ref/drama-skills' / EDITOR_REL
    expected = lock['files_sha256'][EDITOR_REL]
    if hashlib.sha256(editor.read_bytes()).hexdigest() != expected:
        raise SafetyError('上游剪辑工具与项目锁定版本不一致，停止执行')
    return editor

def inspect_output(episode: Path) -> None:
    episode = episode.resolve(strict=True)
    if not episode.is_dir():
        raise SafetyError('剧集目录不存在')
    products = episode / '制作成果'
    output = products / '成片'
    if products.is_symlink() or output.is_symlink():
        raise SafetyError('制作成果或成片目录为符号链接，禁止覆盖；请使用新的隔离渲染目录')
    if products.exists() and not products.is_dir():
        raise SafetyError('制作成果不是目录')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise SafetyError('成片目录已有文件；请先复制剪辑单至新的隔离渲染目录，保留旧版本')

def covers_charset(value: str, required=REQUIRED_GLYPHS) -> bool:
    try:
        ranges=[]
        for item in value.split():
            bits=item.split('-')
            if len(bits)==1:
                lo=hi=int(bits[0],16)
            elif len(bits)==2:
                lo,hi=map(lambda x:int(x,16),bits)
            else:
                return False
            ranges.append((lo,hi))
        return all(any(lo<=cp<=hi for lo,hi in ranges) for cp in required)
    except ValueError:
        return False

def match_font(family: str, env: dict[str,str]) -> tuple[str,str,str]:
    try:
        p=subprocess.run(['fc-match','-f','%{family}\n%{file}\n%{charset}\n',family],
                         env=env,capture_output=True,text=True,timeout=20,check=True)
    except (OSError,subprocess.SubprocessError) as exc:
        raise SafetyError('无法验证中文字幕字体，请检查 Fontconfig') from exc
    parts=p.stdout.split('\n',2)
    if len(parts)!=3 or not parts[0].strip() or not parts[1].strip():
        raise SafetyError('字体匹配结果无效')
    return parts[0].strip(),parts[1].strip(),parts[2].strip()

def font_selection(base: dict[str,str], temp: Path) -> tuple[dict[str,str],str,Path]:
    env=base.copy();env['XDG_CACHE_HOME']=str(temp/'cache')
    (temp/'cache').mkdir()
    for family in ('Noto Sans CJK SC','PingFang SC','Noto Sans SC','WenQuanYi Zen Hei','SimHei'):
        result=match_font(family,env)
        path=Path(result[1])
        if covers_charset(result[2]) and path.is_file():
            return env,result[0].split(',')[0].strip(),path.parent
    raise SafetyError('没有覆盖常用汉字的字幕字体；停止渲染以避免方块字幕')

def with_fontsdir(filter_value: str, directory: Path) -> str:
    escaped=str(directory).replace('\\','/').replace(':',r'\:').replace("'",r"\'")
    pattern=r"ass='(?:\\.|[^'\\])*'"
    result,count=re.subn(pattern,lambda m:m.group(0)+f":fontsdir='{escaped}'",filter_value)
    if count>1:raise SafetyError('字幕滤镜结构异常，停止渲染')
    return result

def run_editor(editor: Path, argv: list[str], family: str, directory: Path) -> int:
    spec=importlib.util.spec_from_file_location('vision_pinned_drama_edit',editor)
    if not spec or not spec.loader:raise SafetyError('无法加载锁定的剪辑工具')
    module=importlib.util.module_from_spec(spec)
    sys.modules[spec.name]=module
    spec.loader.exec_module(module)
    from h3_shot_contract import checker
    module.SEQUENCE_CHECKS = checker()
    module.SUBTITLE_FONT=family
    original_run=module._run
    def run_with_local_font(command):
        command=list(command)
        for i,value in enumerate(command):
            if value in ('-vf','-filter_complex') and i+1<len(command):
                command[i+1]=with_fontsdir(command[i+1],directory)
        return original_run(command)
    module._run=run_with_local_font
    return module.main(argv)

def args_for_guard(argv):
    p=argparse.ArgumentParser(add_help=False)
    p.add_argument('command',nargs='?');p.add_argument('episode',nargs='?')
    p.add_argument('--project-root');p.add_argument('--no-subtitles',action='store_true')
    p.add_argument('--subtitles',choices=('ffmpeg','remotion'),default='ffmpeg')
    p.add_argument('--remotion-workspace');p.add_argument('--remotion-concurrency')
    return p.parse_known_args(argv)[0]

def main(argv=None) -> int:
    argv=list(sys.argv[1:] if argv is None else argv)
    try:
        editor=pinned_editor(PROJECT)
        args=args_for_guard(argv)
        if args.command != 'render' or not args.episode:
            return subprocess.call([sys.executable,str(editor),*argv])
        episode=Path(args.episode)
        inspect_output(episode)
        lock=episode.resolve()/'.drama-render.lock'
        try:
            fd=os.open(lock,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        except FileExistsError as exc:
            raise SafetyError('该剧集已有渲染锁，请先核对是否仍在渲染') from exc
        try:
            os.write(fd,str(os.getpid()).encode('ascii'));os.fsync(fd)
            # Recheck after taking the local lock.
            inspect_output(episode)
            with tempfile.TemporaryDirectory(prefix='vision-drama-font-') as name:
                if not args.no_subtitles and args.subtitles=='ffmpeg':
                    env,family,directory=font_selection(dict(os.environ),Path(name))
                    original_cache=os.environ.get('XDG_CACHE_HOME')
                    os.environ['XDG_CACHE_HOME']=env['XDG_CACHE_HOME']
                    try:return run_editor(editor,argv,family,directory)
                    finally:
                        if original_cache is None:os.environ.pop('XDG_CACHE_HOME',None)
                        else:os.environ['XDG_CACHE_HOME']=original_cache
                return subprocess.call([sys.executable,str(editor),*argv])
        finally:
            os.close(fd)
            lock.unlink()
    except (SafetyError,OSError,KeyError,ValueError) as exc:
        print(f'剪辑安全检查：{exc}',file=sys.stderr)
        return 2

if __name__=='__main__':
    raise SystemExit(main())
