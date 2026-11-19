#!/usr/bin/env python3
"""
fetch.py — 抓取小红书视频笔记的元数据与字幕轨

这是 L1 层：只取平台已有的字幕，约 10 秒完成。
只下载字幕，不下载视频。

用法:
    python3 fetch.py <note_id_or_url> <workdir> [--lang zh]

输出到 <workdir>:
    meta.json              笔记元数据、时长、字幕轨
    subtitle_<lang>.srt    各语言字幕（自动去重相同内容）
"""
import argparse
import json
import re
import subprocess
import sys
import urllib.request
from hashlib import md5
from pathlib import Path

from run_extract import INSTALL_HINT, cli_guidance, cli_payload, fail


def say(msg):
    """面向用户的进度播报，统一前缀便于识别"""
    print(f'▸ {msg}', flush=True)


def parse_note_id(s):
    m = re.search(r'([0-9a-f]{24})', s)
    if not m:
        sys.exit('无法识别笔记 ID，请提供小红书笔记链接或 24 位 ID')
    return m.group(1)


def load_note(note):
    say('正在获取笔记信息…')
    # 优先用完整 URL：部分笔记（带 xsec_token）只有完整链接才能访问，
    # 纯 ID 会走 HTML 解析路径而报「empty noteDetailMap」
    try:
        r = subprocess.run(['xhs', 'read', note, '--json'], stdin=subprocess.DEVNULL,
                           capture_output=True, text=True, timeout=60)
    except OSError:
        fail('CLI_UNAVAILABLE', INSTALL_HINT)
    except subprocess.TimeoutExpired:
        fail('NOTE_TIMEOUT', '读取笔记超过 60 秒，已停止读取；不代表登录失效。', 124)
    d = cli_payload(r.stdout)
    if r.returncode != 0 or d.get('ok') is not True:
        fail(*cli_guidance(d, ('NOTE_READ_FAILED', '读取笔记失败，请核对完整链接或稍后再试；不代表未登录。')))
    items = d.get('data', {}).get('items', [])
    if not items:
        sys.exit('笔记不存在，或返回内容为空')
    return items[0]


def extract_media(note):
    card = note['note_card']
    mv_raw = card.get('video', {}).get('media_v2')
    if not mv_raw:
        return None, {}, None
    mv = json.loads(mv_raw)
    v = mv.get('video', {})
    duration = v.get('duration', 0)
    tracks = {}
    for key, val in (v.get('subtitles') or {}).items():
        if isinstance(val, list):
            for it in val:
                lang = it.get('language') or key
                tracks.setdefault(lang, it['url'])
    streams = (mv.get('stream') or {}).get('h264') or []
    video_url = streams[0]['master_url'] if streams else None
    return duration, tracks, video_url


def human_size(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024:
            return f'{n:.0f} {unit}'
        n /= 1024
    return f'{n:.1f} TB'


def download(url, dest, timeout=60, label=None):
    if label:
        say(label)
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req, timeout=timeout) as r, open(dest, 'wb') as f:
        f.write(r.read())
    return Path(dest).stat().st_size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('note')
    ap.add_argument('workdir')
    ap.add_argument('--lang', default=None, help='只下载指定语言，如 zh / en')
    args = ap.parse_args()

    note_id = parse_note_id(args.note)
    wd = Path(args.workdir)
    wd.mkdir(parents=True, exist_ok=True)

    note = load_note(args.note)
    card = note['note_card']
    duration, tracks, video_url = extract_media(note)

    author = ((card.get('user') or {}).get('nickname')
              or (card.get('user') or {}).get('nick_name'))
    meta = {
        'note_id': note_id,
        'title': card.get('title') or card.get('display_title'),
        'author': author,
        'type': card.get('type'),
        'duration_sec': duration,
        'desc': card.get('desc', ''),
        'subtitle_tracks': list(tracks.keys()),
    }
    print(f'  标题：{meta["title"]}')
    seconds = int(duration) if isinstance(duration, (int, float)) and 0 <= duration < float('inf') else None
    length = f'{seconds//60}分{seconds%60:02d}秒' if seconds is not None else '未知'
    print(f'  作者：{author or "未知"}   时长：{length}')

    # 字幕下载：按内容 hash 去重（zh-CN 与 source 常完全相同）
    (wd / 'meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    seen = {}
    saved = []
    failed = 0
    if tracks:
        say(f'找到 {len(tracks)} 条字幕轨，正在下载…')
    for lang, url in tracks.items():
        if args.lang and not lang.lower().startswith(args.lang.lower()):
            continue
        try:
            raw = urllib.request.urlopen(
                urllib.request.Request(url,
                                       headers={'User-Agent': 'Mozilla/5.0'}),
                timeout=60).read()
        except Exception:
            failed += 1
            print(f'  ⚠️ {lang} 字幕下载失败（网络或字幕链接异常）', flush=True)
            continue
        cues = len(re.findall(rb'-->', raw))
        if not cues:
            failed += 1
            print(f'  ⚠️ {lang} 字幕响应不含有效条目', flush=True)
            continue
        h = md5(raw).hexdigest()
        if h in seen:
            print(f'  {lang}：与 {seen[h]} 内容相同，跳过')
            continue
        seen[h] = lang
        p = wd / f'subtitle_{lang}.srt'
        p.write_bytes(raw)
        print(f'  {lang}：{cues} 条字幕已保存', flush=True)
        saved.append(lang)

    if not saved:
        if failed:
            fail('SUBTITLE_DOWNLOAD_FAILED', '找到了字幕轨，但没有成功取得可用字幕；这不代表未登录。')
        fail('NO_PLATFORM_SUBTITLE', '没有符合要求的平台字幕轨。')
    say('平台字幕已就绪')
    print(f'\n完成。文件在 {wd}/')


if __name__ == '__main__':
    main()
