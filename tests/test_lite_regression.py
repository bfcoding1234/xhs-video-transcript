import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import archive
import diandian_transcript as dd
import export_subtitle as sub
import fetch
import semantic_review as review


class PointTests(unittest.TestCase):
    def test_short_spoken_text_and_tool_name(self):
        self.assertEqual(dd.transcript_body('今天教大家用剪映。'), '今天教大家用剪映。')

    def test_spoken_repetition_and_notes_are_preserved(self):
        text = '再说一遍。\n再说一遍。\n注：这是我的逐字稿示例。'
        self.assertEqual(dd.transcript_body(text), text)

    def test_recognized_service_wrapper(self):
        text = ('根据你引用的笔记，以下是该视频的完整 ASR 逐字稿（按时间顺序，未做总结）：\n'
                '\n---\n\n第一段。\n---\n第二段。\n\n---\n\n注：未附带精确时间戳。')
        self.assertEqual(dd.transcript_body(text), '第一段。\n---\n第二段。')

    def test_non_transcripts_are_rejected(self):
        for text in ('完整逐字稿：\n---\n\n---\n',
                     '这段视频主要讲的是求职。' + '作者总结了三个要点。' * 20,
                     '抱歉，我无法提取口播，可以使用其他工具。',
                     '逐字稿：\n---\n那我就先说到这\n---\n注：剩余内容省略',
                     '前半段口播。\n（内容已截断）'):
            with self.subTest(text=text[:35]), self.assertRaises(dd.DiandianError):
                dd.transcript_body(text)

    @staticmethod
    def history(text):
        return {'data': {'items': [{'message': {'simple_data': {'base_info': {'text': text}}}}]}}

    def poll(self, answers):
        responses = iter(self.history(text) for text in answers)
        with patch.object(dd, 'POLL_MAX', len(answers)), patch.object(dd, 'wait_within'), patch.object(dd, 'say'):
            return dd.poll_transcript(lambda *args, **kwargs: next(responses), 'test', time.monotonic() + 10)

    def test_poll_waits_for_stable_text_without_minimum_length(self):
        self.assertEqual(self.poll(['还在写', '大家好。', '大家好。']), '大家好。')

    def test_poll_refuses_unstable_text_or_summary(self):
        with self.assertRaisesRegex(dd.DiandianError, '稳定'):
            self.poll(['一', '一二', '一二三'])
        with self.assertRaisesRegex(dd.DiandianError, '摘要'):
            self.poll(['这段视频主要讲面试。'] * 2)


class PlatformTests(unittest.TestCase):
    def test_srt_keeps_numeric_cue_and_text_arrows_with_milliseconds(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'sub.srt'
            path.write_bytes(('\ufeff1\r\n00:00:00,240 --> 00:00:00,299\r\n2026\r\n \r\n'
                              '2\r\n00:00:00,300 --> 00:00:01,100\r\nA --> B\r\n').encode())
            cues = sub.parse_srt(path)
        self.assertEqual([c['text'] for c in cues], ['2026', 'A --> B'])
        self.assertEqual([sub.ts(c['start']) for c in cues], ['00:00.240', '00:00.300'])
        self.assertEqual(cues[1]['end'], 1.1)

    def test_metadata_without_video_yields_structured_no_subtitles(self):
        with tempfile.TemporaryDirectory() as root, patch.object(sys, 'argv', ['fetch', '0' * 24, root]), \
                patch.object(fetch, 'load_note', return_value={'note_card': {'type': 'normal'}}), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as errors:
            with self.assertRaises(SystemExit) as exit:
                fetch.main()
            self.assertEqual(exit.exception.code, 1)
            self.assertIn('[NO_PLATFORM_SUBTITLE]', errors.getvalue())
            self.assertIsNone(json.loads((Path(root) / 'meta.json').read_text())['duration_sec'])


class ReviewTests(unittest.TestCase):
    def test_roundtrip_exact_time(self):
        for seconds in (0.24, 62.013, 3770.45):
            self.assertAlmostEqual(review.parse_start(f'[{sub.ts(seconds)}]'), seconds)
        for value in ('NaN', 'inf', '-1', True):
            self.assertIsNone(review.parse_start(value))

    def test_closest_repeated_phrase_is_only_target(self):
        cues = [{'start': 10, 'end': 10.5, 'text': '爱若'}, {'start': 11, 'end': 11.5, 'text': '爱若'}]
        issues = [{'id': 'a', '_start_sec': 11, 'orig': '爱若', 'suggest': 'Aero'}]
        result, applied = review.apply_issues(cues, issues)
        self.assertEqual([c['text'] for c in result], ['爱若', 'Aero'])
        self.assertEqual([c['start'] for c in result], [10, 11])
        self.assertEqual(applied, {'a'})

    def test_ties_and_explicit_time_conflicts_remain_pending(self):
        cues = [{'start': 10, 'text': '相同'}, {'start': 11, 'text': '相同'}, {'start': 15, 'text': '只有这里'}]
        issues = [{'id': 1, '_start_sec': 10.5, 'orig': '相同', 'suggest': '修订'},
                  {'id': 2, '_start_sec': 80, 'orig': '只有这里', 'suggest': '不应修改'}]
        self.assertEqual(review.apply_issues(cues, issues), (cues, set()))

    def test_edits_do_not_cascade_and_nonoverlapping_edits_both_apply(self):
        cues = [{'start': 10, 'text': '甲乙'}]
        issues = [{'id': 1, '_start_sec': 10, 'orig': '甲', 'suggest': '丙'},
                  {'id': 2, '_start_sec': 10, 'orig': '丙', 'suggest': '丁'},
                  {'id': 3, '_start_sec': 10, 'orig': '乙', 'suggest': '戊'}]
        self.assertEqual(review.apply_issues(cues, issues), ([{'start': 10, 'text': '丙戊'}], {1, 3}))


class ArchiveTests(unittest.TestCase):
    def test_uses_full_id_reuses_equal_body_and_versions_changes(self):
        with tempfile.TemporaryDirectory() as root:
            nid = 'abcdef120000000000000001'
            folder = archive.note_folder(root, {'title': '示例', 'note_id': nid})
            name = '带时间字幕_平台识别.md'

            def render(body, title='甲'):
                return f'# {title}\n- 原笔记：{nid}\n## 字幕正文\n\n{body}\n'.encode()

            first, reused = archive.save_version(folder, name, render('原文'))
            self.assertFalse(reused)
            self.assertEqual(archive.note_folder(root, {'title': '改名', 'note_id': nid}), folder)
            self.assertNotEqual(archive.note_folder(root, {'title': '示例', 'note_id': nid[:-1] + '2'}), folder)
            self.assertEqual(archive.save_version(folder, name, render('原文', '乙')), (first, True))
            second, reused = archive.save_version(folder, name, render('更新'))
            self.assertFalse(reused)
            self.assertTrue(second.name.endswith('_v2.md'))
            self.assertEqual(first.read_bytes(), render('原文'))


if __name__ == '__main__':
    unittest.main()
