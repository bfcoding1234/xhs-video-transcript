#!/usr/bin/env python3
"""
naming.py — 交付物的统一命名与目录规则

一个帖子的全部产物共用一个文件夹，文件夹名放共性信息，
文件夹内的文件名只放能区分彼此的部分。这样一眼能看出：

    5分钟精通用AI自动搞定视频剪辑_Avery胡同学_6af45e8d/   ← 共性：这个帖子
        带时间字幕_平台识别.md                            ← 差异：什么部件
        语义审校_带时间.md
        语义审校_记录.md
        原视频.mp4

为什么把前缀移到文件夹：
    之前每个文件都叫 `5分钟精通…_Avery胡同学_6af45e8d_带时间字幕_平台识别.md`，
    在文件列表里前 40 个字符完全一样，真正区分的部分被挤到屏幕外，
    根本没法快速扫读。共性归目录，个性归文件名，一眼就分得清。
"""
import re

# 标题、作者各自的长度上限（字符），避免文件夹名过长
TITLE_MAX = 24
AUTHOR_MAX = 12
ID_LEN = 8

# 文件名里不允许出现的字符
ILLEGAL = r'[\\/:*?"<>|\r\n\t]'

# 交付文件的规范部件名。export/semantic/render 按这个写，
# collect 按这个收，两边不会对不上。
MD_PARTS = [
    '带时间字幕_平台识别',
    '语义审校_带时间',
    '语义审校_记录',
    '带时间字幕_本地识别',
    '纯文字字幕_本地识别',
    '终稿_带时间',
    '终稿_纯文字',
    '仲裁报告',
    '合并报告',
    '点点逐字稿_纯文字',
]

# (工作目录里的中间产物名, 交付用部件名)
MEDIA_PARTS = [
    ('video.mp4', '原视频'),
    ('audio.wav', '分离音频'),
]


def _clean(text, limit):
    """只保留中英文数字，去掉标点、emoji、空格，再截断。"""
    if not text:
        return ''
    text = re.sub(ILLEGAL, '', str(text))
    text = ''.join(ch for ch in text if ch.isalnum())
    return text[:limit]


def post_folder(meta):
    """文件夹名：标题_作者_短ID。缺字段时自动跳过，不留多余下划线。"""
    meta = meta or {}
    parts = []
    title = _clean(meta.get('title'), TITLE_MAX)
    author = _clean(meta.get('author'), AUTHOR_MAX)
    note_id = _clean(meta.get('note_id'), ID_LEN)
    if title:
        parts.append(title)
    if author:
        parts.append(author)
    if note_id:
        parts.append(note_id)
    return '_'.join(parts) or 'xiaohongshu'


def deliver_name(part, ext):
    """文件夹内的文件名：只放差异部分。

    保留 `deliver_name` 这个名字和 meta 位置的历史调用已被移除，
    现在只接受部件名和扩展名，避免又拼回长前缀。
    """
    return f'{part}.{ext}'


if __name__ == '__main__':
    demo = {'title': '5分钟精通：用AI自动搞定视频剪辑🎬',
            'author': 'Avery胡同学',
            'note_id': '6af45e8d000000002b01d2ab'}
    print(post_folder(demo) + '/')
    for p in MD_PARTS[:3]:
        print('  ' + deliver_name(p, 'md'))
    print('  ' + deliver_name('原视频', 'mp4'))
    print(post_folder({}) + '/   ← 缺元信息时的兜底')
