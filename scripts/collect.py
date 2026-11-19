#!/usr/bin/env python3
"""
collect.py — 把工作目录里的产物收进「一个帖子一个文件夹」

用法:
    python3 collect.py <workdir> [--dest DIR]

目录结构:
    <dest>/5分钟精通用AI自动搞定视频剪辑_Avery胡同学_6af45e8d/
        带时间字幕_平台识别.md
        语义审校_带时间.md
        语义审校_记录.md
        带时间字幕_本地识别.md
        纯文字字幕_本地识别.md
        终稿_带时间.md
        终稿_纯文字.md
        仲裁报告.md
        原视频.mp4
        分离音频.wav

共性信息（标题/作者/ID）只出现在文件夹名里，文件名只放差异部分，
这样在文件列表里能一眼扫到「这是哪个部件的」，不会被重复前缀挡住。

只收 MD_PARTS / MEDIA_PARTS 里登记过的规范部件名，
工作目录里的 subtitle_*.srt、asr.json、disputes.json 等中间文件不交付。
"""
import argparse
import json
from pathlib import Path

from archive import ArchiveError, note_folder, save_version
from naming import MD_PARTS, MEDIA_PARTS


def say(msg):
    print(f'▸ {msg}', flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workdir')
    ap.add_argument('--dest', default=None,
                    help='交付根目录，默认用工作目录的上级')
    ap.add_argument('--archive-folder', help='有多个已验证目录时，指定用户选中的目录名')
    args = ap.parse_args()

    wd = Path(args.workdir)
    files = [(wd / f'{part}.md', f'{part}.md') for part in MD_PARTS]
    files += [(wd / source, f'{part}{Path(source).suffix}') for source, part in MEDIA_PARTS]
    files = [(src, name) for src, name in files if src.is_file() and src.stat().st_size]
    if not files:
        raise SystemExit('工作目录里没有可交付的文件。')

    try:
        meta = json.loads((wd / 'meta.json').read_text(encoding='utf-8'))
        if not isinstance(meta, dict):
            raise ArchiveError('笔记元信息格式不正确，未归档。')
        out = note_folder(Path(args.dest) if args.dest else wd.parent, meta, args.archive_folder)
        say('正在整理交付文件…')
        for src, name in files:
            path, reused = save_version(out, name, src)
            print(f'  {"已复用" if reused else "已保存"} {path}')
    except (ArchiveError, OSError, ValueError) as e:
        raise SystemExit(f'归档无法完成：{e}') from None


if __name__ == '__main__':
    main()
