#!/usr/bin/env python3
"""
semantic_review.py — L2 语义审校：让模型通读字幕、找出可疑处

用法:
    python3 semantic_review.py <workdir> [--review review.json] [--outdir DIR]

背景（为什么需要这一层）:
    L1 只是把平台字幕原样拿出来；L3 的 diff 只比较两份机器结果的差异，
    它回答「哪里不一样」，不回答「哪句读不通、哪个词被听错」。
    实测会漏掉整类问题：
      - 同一个专有名词在不同时间点被识别成不同写法（aro / a r o / aero）
      - 只有语义常识才能发现的错（promotion → Remotion）
      - 完全不成句的碎片（口部可重塑 → 口误和重说）
    所以需要一层真正「读懂内容」的检查。

输入:
    review.json —— 模型通读后产出的可疑点清单，每条含
        start(秒) / orig(原文) / suggest(建议改法) / category / confidence / reason

    可选 pending 数组 —— 待核对项，同样记录定位和疑点，不参加替换。

产出（两个文件）:
    语义审校_带时间.md    已按建议修订，保留时间点
    语义审校_记录.md      已替换与待核对分列

设计原则:
    修订只作用于「L2 审校版」文件，**绝不回写 subtitle_zh-CN.srt**。
    L1 的原始字幕必须保持原样，否则就失去了「真实字幕」的价值，
    也没法再跟 L3/L4 的仲裁结果对照。
"""
import argparse
import json
import math
import re
from pathlib import Path

try:
    from naming import deliver_name
    from export_subtitle import parse_srt, ts
except ImportError:
    import sys
    sys.path.insert(0, str(Path(__file__).parent))
    from naming import deliver_name
    from export_subtitle import parse_srt, ts

CONF_ZH = {'high': '高', 'medium': '中', 'low': '低'}
CATEGORY_ORDER = ['专有名词', '同音字', '分词错误', '语句不通', '断句', '无法确定']


def parse_start(v):
    """Parse numeric seconds or [H:]MM:SS[.mmm], optionally bracketed."""
    if isinstance(v, bool):
        return None
    s = str(v).strip().removeprefix('[').removesuffix(']')
    m = re.fullmatch(r'(?:(\d+):)?([0-5]?\d):([0-5]\d)([.,]\d{1,3})?', s)
    if m:
        h, minutes, seconds, fraction = m.groups()
        return (int(h or 0) * 3600 + int(minutes) * 60 + int(seconds)
                + float((fraction or '.0').replace(',', '.')))
    try:
        value = float(s)
    except ValueError:
        return None
    return value if math.isfinite(value) and value >= 0 else None


def say(msg):
    print(f'▸ {msg}', flush=True)


def apply_issues(cues, issues):
    """Locate edits on the original cues; ties, conflicts and overlaps stay pending."""
    applied = set()
    edits = {}
    for it in issues:
        orig, sugg = it.get('orig', ''), it.get('suggest', '')
        if (it['id'] in applied or not isinstance(orig, str) or not orig.strip()
                or not isinstance(sugg, str) or not sugg.strip() or orig == sugg):
            continue
        sec = it.get('_start_sec')
        if sec is None and it.get('start') not in (None, ''):
            continue  # An invalid explicit timestamp is not missing.
        candidates = []
        for i, cue in enumerate(cues):
            text = cue['text']
            pos = text.find(orig)
            if pos < 0:
                continue
            distance = abs(sec - cue['start']) if sec is not None else 0
            if distance <= 1.5:
                # A cue containing repeated occurrences is still a candidate;
                # if closest, it must not be bypassed in favor of a farther cue.
                candidates.append((distance, i, pos, text.find(orig, pos + 1) >= 0))
        if not candidates:
            continue
        candidates.sort()
        distance, i, start, repeated = candidates[0]
        if repeated or (len(candidates) > 1 and math.isclose(distance, candidates[1][0], abs_tol=1e-9)):
            continue
        end = start + len(orig)
        spans = edits.setdefault(i, [])
        if any(start < b and a < end for a, b, _ in spans):
            continue
        spans.append((start, end, sugg))
        applied.add(it['id'])

    out = [dict(c) for c in cues]
    for i, spans in edits.items():
        text = out[i]['text']
        for start, end, replacement in sorted(spans, reverse=True):
            text = text[:start] + replacement + text[end:]
        out[i]['text'] = text
    return out, applied


def build_report(meta, issues, applied, pending, total):
    title = meta.get('title') or '小红书视频'
    out = [f'# {title} — 语义审校记录\n',
           '模式：脚本辅助审校（模型判断，脚本应用）；未重听原音。\n']
    if meta:
        out.append('| 项目 | 值 |')
        out.append('|---|---|')
        out.append(f"| 原笔记 | {meta.get('note_id')} |")
        out.append(f"| 作者 | {meta.get('author')} |")
        out.append(f"| 通读字幕条数 | {total} |")
        out.append(f'| 发现可疑处 | {len(issues)} |')
        out.append(f'| 已修订 | {len(applied)} |')
        out.append('')

    out.append('## 这份清单是怎么来的\n')
    out.append('模型通读了平台字幕全文，逐句判断哪里「读不通、不像人话、'
               '专有名词可疑」。\n'
               '每条都标了置信度：**高**表示有把握、可直接采纳；'
               '**中**表示方向对但细节需确认；**低**表示只能猜、建议截图核对。\n')

    # 按类别统计
    cats = {}
    for it in issues:
        cats[it.get('category', '其他')] = cats.get(it.get('category', '其他'), 0) + 1
    out.append('## 问题分类\n')
    out.append('| 类别 | 处数 |')
    out.append('|---|---|')
    for c in CATEGORY_ORDER:
        if c in cats:
            out.append(f'| {c} | {cats[c]} |')
    for c, n in cats.items():
        if c not in CATEGORY_ORDER:
            out.append(f'| {c} | {n} |')
    out.append('')

    out.append('## 已替换\n')
    if not applied:
        out.append('无。\n')
    for it in issues:
        if it['id'] not in applied:
            continue
        conf = CONF_ZH.get(it.get('confidence'), it.get('confidence'))
        sec = it.get('_start_sec')
        disp = ts(sec) if sec is not None else str(it.get('start'))
        out.append(f'### {it["id"]}. `[{disp}]` '
                   f'{it.get("category")} · 置信度{conf}\n')
        out.append(f'- 原文：{it.get("orig")}')
        out.append(f'- 建议：**{it.get("suggest")}**')
        out.append(f'- 理由：{it.get("reason")}')
        out.append('')

    out.append('## 待核对\n')
    unchecked = [it for it in issues if it['id'] not in applied] + pending
    if not unchecked:
        out.append('无。')
    for it in unchecked:
        sec = parse_start(it.get('start'))
        disp = ts(sec) if sec is not None else '无定位'
        prefix = '未匹配，未替换' if it in issues else '保留原文，未替换'
        out.append(f'- `[{disp}]` {it.get("orig", "")}（{prefix}）')
        if it.get('suggest'):
            out.append(f'  - 分歧或建议：{it["suggest"]}')
        if it.get('reason'):
            out.append(f'  - 待核原因：{it["reason"]}')
    out.append('')

    return '\n'.join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('workdir')
    ap.add_argument('--review', default='review.json')
    ap.add_argument('--srt', default='subtitle_zh-CN.srt')
    ap.add_argument('--outdir', default=None)
    args = ap.parse_args()

    wd = Path(args.workdir)
    outdir = Path(args.outdir) if args.outdir else wd
    outdir.mkdir(parents=True, exist_ok=True)

    say('正在按审校结果修订字幕…')
    src = wd / args.srt
    if not src.exists():
        raise SystemExit(f'  找不到 {src}')
    cues = parse_srt(src)

    rv = wd / args.review
    if not rv.exists():
        raise SystemExit(f'  找不到 {rv}，模型还没通读字幕')
    data = json.loads(rv.read_text(encoding='utf-8'))
    candidates = data.get('issues') or []
    issues = [it for it in candidates if it.get('confidence') != 'low']
    pending = (data.get('pending') or []) + [it for it in candidates if it.get('confidence') == 'low']
    for it in issues:
        it['_start_sec'] = parse_start(it.get('start'))

    meta = {}
    if (wd / 'meta.json').exists():
        meta = json.loads((wd / 'meta.json').read_text(encoding='utf-8'))

    revised, applied = apply_issues(cues, issues)

    def header(kind):
        h = [f'# {meta.get("title", "小红书视频")} — 语义审校（{kind}）\n']
        if meta:
            h.append(f"- 原笔记：{meta.get('note_id')}")
            h.append(f"- 作者：{meta.get('author')}")
            h.append(f'- 来源：平台字幕 + 模型通读审校，'
                     f'修订 {len(applied)}/{len(issues)} 处')
            h.append('- 说明：这是**审校修订版**，平台原始字幕另存，未改动')
            h.append('')
        return h

    timed = header('带时间')
    timed.append('## 正文\n')
    for c in revised:
        timed.append(f'**[{ts(c["start"])}]** {c["text"]}')
    timed_name = deliver_name('语义审校_带时间', 'md')
    (outdir / timed_name).write_text('\n'.join(timed) + '\n', encoding='utf-8')

    report_name = deliver_name('语义审校_记录', 'md')
    (outdir / report_name).write_text(
        build_report(meta, issues, applied, pending, len(cues)), encoding='utf-8')

    print(f'  已替换 {len(applied)} 处，待核对 {len(issues) - len(applied) + len(pending)} 处')
    print(f'  {outdir / timed_name}')
    print(f'  {outdir / report_name}')
    if len(applied) < len(issues):
        miss = [it['id'] for it in issues if it['id'] not in applied]
        print(f'  注意：{len(miss)} 处未能自动应用（id={miss}），见清单')


if __name__ == '__main__':
    main()
