#!/usr/bin/env python3
"""Check ordered dialogue coverage in screenplay versus H3 storyboard fields.

This is a textual preflight, not proof that generated audio contains the lines.
A line may be split into adjacent fragments by the same speaker, e.g. across a
continuation boundary. Script, delivery modes and audiovisual timing still
need creative review.
"""
import argparse
from pathlib import Path
import re
import sys


def compact_runs(items):
    runs = []
    for speaker, text in items:
        if runs and runs[-1][0] == speaker:
            runs[-1] = (speaker, runs[-1][1] + text)
        else:
            runs.append((speaker, text))
    return runs


def read_script(script, visual):
    names = re.findall(r'^## 人物 · (.+?)\s*$', visual, re.M)
    if not names:
        raise ValueError('视觉设定未列人物')
    for line in script.splitlines():
        # Ignore explicit sound, screen-text and continuity cues, but do not
        # silently skip a dialogue speaker missing from the visual document.
        stripped = line.strip()
        if not stripped or stripped.startswith('#'):
            continue
        if stripped.startswith('[') and not re.match(r'^\[(?:OS|VO)\]', stripped):
            continue
        match = re.match(r'^(?:\[(?:OS|VO)\]\s*)?([^\s：:（）]{1,15})(?:（[^）]*）)?[：:]', stripped)
        if match and match.group(1) not in names:
            raise ValueError(f'剧本说话人未在视觉设定登记：{match.group(1)}')
    alternatives = '|'.join(re.escape(x) for x in sorted(names, key=len, reverse=True))
    pattern = re.compile(r'^\s*(?:\[(?:OS|VO)\]\s*)?(' + alternatives + r')(?:（[^）]*）)?[：:]\s*(.+?)\s*$', re.M)
    return compact_runs([(m.group(1), m.group(2)) for m in pattern.finditer(script)])


def read_board(storyboard):
    lines = re.findall(r'^- H3台词：(.*)$', storyboard, re.M)
    if not lines:
        raise ValueError('分镜缺少 H3台词 字段')
    pattern = re.compile(r'([^；;（(]+)[（(](?:on|voiceover|off_screen)[）)]「([^」]+)」')
    items = []
    for line in lines:
        if line.strip() == '无':
            continue
        found = [(m.group(1).strip(), m.group(2)) for m in pattern.finditer(line)]
        remainder = pattern.sub('', line).strip(' ；;')
        if not found or remainder:
            raise ValueError('存在无法解析的 H3台词 字段')
        items.extend(found)
    return compact_runs(items)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('episode_dir', type=Path)
    args = ap.parse_args()
    try:
        script, visual, board = [(args.episode_dir / name).read_text(encoding='utf-8')
                                 for name in ('剧本.md', '视觉设定.md', '分镜.md')]
        expected, actual = read_script(script, visual), read_board(board)
        if not expected:
            raise ValueError('剧本未找到可识别的角色对白')
        if expected != actual:
            for i in range(max(len(expected), len(actual))):
                e = expected[i] if i < len(expected) else None
                a = actual[i] if i < len(actual) else None
                if e != a:
                    print(f'对白覆盖不一致，第 {i+1} 组：剧本 {e!r}；分镜 {a!r}', file=sys.stderr)
                    break
            return 1
        print(f'对白顺序、说话人及全文一致：{len(expected)} 组；实际音频仍须审核。')
        return 0
    except (OSError, ValueError) as exc:
        print(f'对白检查失败：{exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
