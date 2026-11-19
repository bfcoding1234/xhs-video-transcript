#!/usr/bin/env python3
"""
export_subtitle.py — 导出平台字幕或本地识别字幕

用法:
    python3 export_subtitle.py <workdir> [--lang zh-CN] [--outdir .]

产出:
    平台字幕：仅带时间戳，一条字幕一行
    本地识别：带时间戳及按标点和停顿分段的纯文字

设计原则:
    - 这些是「原始字幕」而非「整理稿」，所以不做错别字纠正、
      不做术语修正、不做润色。原文什么样就是什么样。
    - 术语存疑等在讲解总结里说明，不要污染字幕文件本身。
"""
import argparse
import json
import re
from pathlib import Path

try:
    from naming import deliver_name
except ImportError:                                  # 从其他目录调用时兜底
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from naming import deliver_name

# 句末标点：遇到就断段。
# 注意 whisper 输出的是**半角**标点（,.?!），平台字幕是全角，
# 只认全角会让 ASR 版本几乎切不开，最后只剩长度兜底一个条件。
SENT_END = '。！？!?….;；'
# 停顿阈值（秒）：两条字幕间隔超过它就另起一段
PAUSE_GAP = 1.5
# 字符数阈值：平台字幕大量条目不带句末标点，光靠标点会切出几千字的巨块，
# 所以累积到这个长度也断段，保证纯文字版是可读的段落而不是一坨。
MAX_PARA = 110


def say(msg):
    print(f'▸ {msg}', flush=True)


_SRT_TIME = r'(\d+):([0-5]\d):([0-5]\d)[,.](\d{1,3})'
_SRT_RANGE = re.compile(rf'^\s*{_SRT_TIME}\s*-->\s*{_SRT_TIME}(?:\s+.*)?$')


def _srt_seconds(parts):
    h, m, s, fraction = parts
    return int(h) * 3600 + int(m) * 60 + int(s) + int(fraction.ljust(3, '0')) / 1000


def parse_srt(path):
    raw = Path(path).read_text(encoding='utf-8-sig', errors='replace')
    out = []
    for block in re.split(r'\n(?:[^\S\n]*\n)+', raw.strip()):
        lines = block.splitlines()
        if not lines:
            continue
        # Only a numeric line before the timestamp is an index. After it,
        # numeric text and even literal arrows belong to the spoken subtitle.
        index = int(lines[0].strip().isdigit())
        if index >= len(lines):
            continue
        timing = _SRT_RANGE.fullmatch(lines[index])
        if not timing:
            continue
        text = ' '.join(lines[index + 1:]).strip()
        if text:
            parts = timing.groups()
            out.append({'start': _srt_seconds(parts[:4]), 'end': _srt_seconds(parts[4:]),
                        'text': text})
    return out


def ts(sec):
    total_seconds, ms = divmod(round(sec * 1000), 1000)
    h, remainder = divmod(total_seconds, 3600)
    m, s = divmod(remainder, 60)
    prefix = f'{h}:{m:02d}' if h else f'{m:02d}'
    return f'{prefix}:{s:02d}.{ms:03d}'


def pick_source(wd, lang, source):
    """按 source 选择字幕来源。

    source='platform'  只找平台字幕 subtitle_*.srt
    source='asr'       只用本地识别 asr.srt
    source='auto'      平台优先，没有就退到 asr
    """
    platform = [wd / f'subtitle_{lang}.srt', wd / 'subtitle_zh-CN.srt']
    platform += sorted(wd.glob('subtitle_*.srt'))
    asr = [wd / 'asr.srt']
    order = {'platform': platform,
             'asr': asr,
             'auto': platform + asr}[source]
    for p in order:
        if p.exists() and p.stat().st_size > 0:
            return p
    return None


def out_names(source):
    """生成两份字幕的文件名。

    共性信息（标题/作者/ID）已上移到文件夹名，这里只放差异部分：
    用「平台识别 / 本地识别」区分来源，比看 L1/L2 更直观。
    """
    label = {'platform': '平台识别', 'asr': '本地识别',
             'auto': '平台识别'}[source]
    return (deliver_name(f'带时间字幕_{label}', 'md'),
            deliver_name(f'纯文字字幕_{label}', 'md'))


def split_sentences(text):
    """把一条字幕拆成句子，标点留在句尾。"""
    out, buf = [], ''
    for ch in text:
        buf += ch
        if ch in SENT_END:
            if buf.strip():
                out.append(buf.strip())
            buf = ''
    if buf.strip():
        out.append(buf.strip())
    return out


def build_plain(cues):
    """去掉时间戳，先拆句、再按长度和停顿拼成可读段落。

    两个来源的字幕形态差别很大，必须兼顾：
    - 平台字幕：条目短、多不带句号 → 靠长度和停顿攒段
    - ASR 字幕：一条就是一整句、条目间只隔 0.2 秒 → 靠长度上限切段
    先统一拆成句子，再按 MAX_PARA 和长停顿拼段，两边都能得到均匀段落。
    """
    units = []                       # (句子, 该句结束处到下一条的间隔)
    for i, c in enumerate(cues):
        sents = split_sentences(c['text'])
        nxt = cues[i + 1] if i + 1 < len(cues) else None
        gap = 0.0
        if nxt and c['end'] is not None:
            gap = max(0.0, nxt['start'] - c['end'])
        for j, s in enumerate(sents):
            # 只有该字幕的最后一句才继承「到下一条的间隔」
            units.append((s, gap if j == len(sents) - 1 else 0.0))

    paras, buf = [], ''
    for s, gap in units:
        if buf and len(buf) + len(s) > MAX_PARA:
            paras.append(buf.strip())
            buf = ''
        buf += s
        if gap >= PAUSE_GAP:
            paras.append(buf.strip())
            buf = ''
    if buf.strip():
        paras.append(buf.strip())
    return [p for p in paras if p]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workdir')
    ap.add_argument('--lang', default='zh-CN')
    ap.add_argument('--outdir', default=None)
    ap.add_argument('--source', default='auto',
                    choices=['auto', 'platform', 'asr'],
                    help='platform=平台字幕，asr=本地识别结果，auto=平台优先')
    args = ap.parse_args()

    wd = Path(args.workdir)
    outdir = Path(args.outdir) if args.outdir else wd
    say('正在导出字幕文件…')

    src = pick_source(wd, args.lang, args.source)
    if not src:
        raise SystemExit('  没有可用字幕文件，先跑 fetch.py 或 asr.py')
    cues = parse_srt(src)
    if not cues:
        raise SystemExit(f'  {src.name} 解析不出字幕条目')

    meta = {}
    meta_path = wd / 'meta.json'
    if meta_path.exists():
        meta = json.loads(meta_path.read_text(encoding='utf-8'))

    via_asr = src.name == 'asr.srt'
    timed_name, plain_name = out_names('asr' if via_asr else 'platform')
    origin = ('本地音频识别（whisper large-v3，未做人工校订）'
              if via_asr else '平台自动识别字幕，未做人工校订')

    title = meta.get('title', '小红书视频')
    header = [f'# {title} — 带时间字幕\n']
    if meta:
        header.append(f"- 原笔记：{meta.get('note_id')}")
        header.append(f"- 作者：{meta.get('author')}")
        header.append(f"- 时长：{meta.get('duration_sec')} 秒")
        header.append(f"- 来源：{src.name}（{origin}）")
        header.append('')

    timed = list(header)
    timed.append('## 字幕正文\n')
    for c in cues:
        timed.append(f'**[{ts(c["start"])}]** {c["text"]}')
    (outdir / timed_name).write_text('\n'.join(timed) + '\n',
                                     encoding='utf-8')

    if not via_asr:
        print(f'  共 {len(cues)} 条字幕')
        print(f'  {outdir / timed_name}')
        return

    plain = [f'# {title} — 纯文字字幕\n']
    if meta:
        plain.append(f"- 原笔记：{meta.get('note_id')}")
        plain.append(f"- 作者：{meta.get('author')}")
        plain.append(f"- 来源：{src.name}（去时间戳，按标点和停顿分段）")
        plain.append('')
    plain.append('## 正文\n')
    paras = build_plain(cues)
    plain.append('\n\n'.join(paras))
    (outdir / plain_name).write_text('\n'.join(plain) + '\n',
                                     encoding='utf-8')

    print(f'  共 {len(cues)} 条字幕 / {len(paras)} 段纯文字')
    print(f'  {outdir / timed_name}')
    print(f'  {outdir / plain_name}')


if __name__ == '__main__':
    main()
