"""Identify note folders and publish immutable, versioned deliverables."""
import filecmp
import os
from pathlib import Path
import re
import shutil
import tempfile

from naming import post_folder

ID = re.compile(r'[0-9a-f]{24}')
ID_FIELD = re.compile(r'(?:\s*- 原笔记\s*[：:]\s*([0-9a-f]{24})|\s*\| 原笔记 \|\s*([0-9a-f]{24})\s*\|)\s*')
BODY_MARKERS = {'带时间字幕_平台识别.md': '## 字幕正文',
                '点点逐字稿_纯文字.md': '## 正文'}


class ArchiveError(Exception):
    pass


def header_id(path):
    with path.open(encoding='utf-8-sig', errors='replace') as stream:
        for line in stream.read(8192).splitlines():
            if line.startswith('## '):
                break
            match = ID_FIELD.fullmatch(line)
            if match:
                return match.group(1) or match.group(2)
    return None


def note_folder(root, meta, selected=None, create=True):
    root = Path(root).expanduser().resolve()
    note_id = meta.get('note_id')
    if not isinstance(note_id, str) or not ID.fullmatch(note_id):
        raise ArchiveError('缺少有效的完整笔记 ID，未归档。')
    matches = []
    if root.exists():
        for folder in sorted(root.iterdir()):
            if folder.is_symlink() or not folder.is_dir():
                continue
            ids = {header_id(p) for p in folder.glob('*.md')
                   if not p.is_symlink() and p.is_file()}
            ids.discard(None)
            if note_id in ids:
                if ids != {note_id}:
                    raise ArchiveError(f'目录含多个笔记 ID，请人工核对后再归档：{folder}')
                matches.append(folder)
    if selected is not None:
        for folder in matches:
            if folder.name == selected:
                return folder
        raise ArchiveError('指定目录不是此笔记已验证的交付目录，未归档。')
    if len(matches) > 1:
        choices = '\n'.join(f'  - {p.name!r}' for p in matches)
        raise ArchiveError(f'同一笔记有多个交付目录，请选择后以 --archive-folder 指定目录名：\n{choices}')
    if matches:
        return matches[0]
    if not create:
        return None

    root.mkdir(parents=True, exist_ok=True)
    preferred = post_folder(meta)
    # An unverified directory may belong to another note with the same short ID.
    for i in range(10000):
        name = preferred if i == 0 else f'{preferred}_{note_id}' + (f'_{i}' if i > 1 else '')
        dest = root / name
        try:
            dest.mkdir()
            return dest
        except FileExistsError:
            continue
    raise ArchiveError('未找到可用的交付目录名，未归档。')


def _body(path, marker):
    text = path.read_text(encoding='utf-8')
    _, found, body = text.partition(f'\n{marker}\n')
    return body if found and body.strip() else None


def save_version(folder, name, content):
    """Stage beside the destination, then publish atomically without replacing it."""
    target = folder / name
    versioned = re.compile(re.escape(target.stem) + r'_v([2-9]|[1-9]\d+)' + re.escape(target.suffix))
    marker = BODY_MARKERS.get(name)

    with tempfile.TemporaryDirectory(prefix='.xhs-stage-', dir=folder) as stage:
        staged = Path(stage) / name
        if isinstance(content, bytes):
            staged.write_bytes(content)
        else:
            shutil.copyfile(content, staged)
        body = _body(staged, marker) if marker else None

        while True:
            versions = []
            for entry in folder.iterdir():
                if entry.name == name:
                    versions.append((1, entry))
                else:
                    match = versioned.fullmatch(entry.name)
                    if match:
                        versions.append((int(match.group(1)), entry))
            for _, entry in sorted(versions):
                if entry.is_symlink() or not entry.is_file():
                    continue
                if filecmp.cmp(staged, entry, shallow=False):
                    return entry, True
                if body is not None and header_id(entry) == header_id(staged):
                    try:
                        if _body(entry, marker) == body:
                            return entry, True
                    except UnicodeError:
                        pass
            number = max((n for n, _ in versions), default=0) + 1
            output = target if number == 1 else target.with_name(f'{target.stem}_v{number}{target.suffix}')
            try:
                os.link(staged, output)
            except FileExistsError:
                continue
            return output, False
