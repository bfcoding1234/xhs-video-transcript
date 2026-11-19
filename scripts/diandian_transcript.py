#!/usr/bin/env python3
"""
diandian_transcript.py — L5 点点转录：纯 CLI 获取小红书视频逐字稿

用法:
    python3 diandian_transcript.py <note_id> [--outdir DIR] [--cookie FILE] [--prompt TEXT]

原理（2026-11-18 实测打通的三接口流程）:
    1) POST /api/sns/agentspark/web/note/detail   注册引用笔记（必须先做，
       否则 send/ai 里的引用不生效，点点会把问题当普通闲聊回答）
       副产物: 返回 xsec_token，可用于直接访问笔记页
    2) POST /api/sns/web/search/send/ai           发起提问（带 multi_param_query 引用）
    3) GET  /api/sns/web/v1/dqa/history/detail    轮询取回答

签名:
    用 xhshow 库（随 xiaohongshu-cli 安装）本地生成 XYS_ 签名。
    使用能直接导入 xhshow 的 Python 解释器（通常是安装 xhs CLI 的环境）。

Cookie:
    默认复用 xhs CLI 的登录态（~/.xiaohongshu-cli/cookies.json）。
    本地文件正常不代表登录仍有效；遇到明确的认证或验证码错误立即停止。
"""
import argparse
import json
import re
import subprocess
import sys
import time
import uuid as uuidlib
from pathlib import Path

try:
    from archive import ArchiveError, note_folder, save_version
    from naming import deliver_name
except ImportError:
    sys.path.insert(0, str(Path(__file__).parent))
    from archive import ArchiveError, note_folder, save_version
    from naming import deliver_name

COOKIE_JSON = Path.home() / '.xiaohongshu-cli/cookies.json'
LOGIN_HELP = ('请先用浏览器登录小红书网页版，再运行 `xhs login` 导入浏览器登录态；'
              '如果无法导入，运行 `xhs login --qrcode` 并用小红书 App 扫码确认。'
              '完成后可运行 `xhs status` 检查。')

# 提取逐字稿的标准提示词（实测有效：不要总结/省略/改写）
DEFAULT_PROMPT = ('请提取这个视频的完整逐字稿，要求：保留全部口语内容，'
                  '按时间顺序，不要总结、不要省略、不要改写，直接输出原文。')

REQUEST_TIMEOUT = 15
CONNECT_TIMEOUT = 5
REQUEST_GAP = 3
REQUEST_BUDGET = 165  # 内部网络总预算；外层仍负责整个进程的限时
POLL_TIMEOUT = 120
POLL_INTERVAL = 10
POLL_MAX = 12
# No verified completion flag in the history payload. Require the same answer
# in consecutive polls, but never label that as proof of a complete transcript.
STABLE_POLLS = 2


class DiandianError(Exception):
    """仅携带脚本生成的、适合显示给用户的错误。"""


class DeadlineExpired(DiandianError):
    pass


class RequestTimeout(DiandianError):
    pass


def say(msg):
    print(f'▸ {msg}', flush=True)


def load_cookies(cookie_file):
    """读取并检查本地登录文件，不显示原始内容。"""
    p = Path(cookie_file)
    login_help = LOGIN_HELP
    if p != COOKIE_JSON:
        login_help += ' 使用自定义 `--cookie` 时，请改指向有效登录文件，或省略该选项。'
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
    except FileNotFoundError:
        raise DiandianError(f'找不到点点登录文件。{login_help}') from None
    except OSError:
        raise DiandianError(f'无法读取点点登录文件；请检查路径和读取权限。{login_help}') from None
    except (ValueError, RecursionError):
        raise DiandianError(f'点点登录文件不是有效的 JSON。{login_help}') from None
    if not isinstance(data, dict):
        raise DiandianError(f'点点登录文件格式错误（应为 JSON 对象）。{login_help}')

    skip = {'saved_at', 'last_web_session', 'customerClientId', '_gray_did'}
    cookies = {}
    for key, value in data.items():
        key = key.strip()
        if key in skip or not isinstance(value, str) or not value.strip():
            continue
        value = value.strip()
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key) or not re.fullmatch(
                r'[!-:<-~]+', value):  # 禁止控制符、分号及异常的头字段内容
            raise DiandianError(f'点点登录文件含无效的 Cookie 字段。{login_help}')
        cookies[key] = value
    if not cookies.get('web_session') or not cookies.get('a1'):
        raise DiandianError(f'点点登录文件缺少有效的登录字段。{login_help}')
    return cookies


def remaining(deadline):
    left = deadline - time.monotonic()
    if left <= 0:
        raise DeadlineExpired('点点处理超时，未取得逐字稿。')
    return left


def wait_within(delay, deadline):
    time.sleep(min(delay, remaining(deadline)))
    remaining(deadline)


def checked_response(status, raw):
    if status in (461, 471):
        raise DiandianError('点点要求验证码或安全验证。请在浏览器手动完成验证后再试。')
    if status == 401:
        raise DiandianError(f'点点未接受当前登录态。{LOGIN_HELP}')
    try:
        out = json.loads(raw)
    except (ValueError, RecursionError):
        out = None
    code = out.get('code') if isinstance(out, dict) else None
    if (isinstance(out, dict)
            and (not 200 <= status < 300 or code not in (0, '0') or out.get('success') is False)):
        error = out.get('error')
        error = error if isinstance(error, dict) else {}
        kind = error.get('code')
        messages = (out.get('msg'), out.get('message'), error.get('message'))
        if kind == 'verification_required' or any(
            isinstance(text, str) and any(word in text for word in ('验证码', '请完成验证', 'captcha'))
            for text in messages
        ):
            raise DiandianError('点点要求验证码或安全验证。请在浏览器手动完成验证后再试。')
        if code in (-100, '-100') or kind == 'not_authenticated' or any(
            isinstance(text, str) and any(word in text for word in ('未登录', '请先登录', '登录已失效', '登录过期'))
            for text in messages
        ):
            raise DiandianError(f'点点提示需要登录或刷新登录态。{LOGIN_HELP}')
        if code in (300012, '300012') or kind == 'ip_blocked':
            raise DiandianError('点点限制了当前访问，本次已停止，不会自动重试。')
    if status == 403:
        raise DiandianError('点点拒绝访问（HTTP 403）；无法仅凭此确认登录失效。')
    if status == 429:
        raise DiandianError('点点请求受到频率限制（HTTP 429），本次已停止。')
    if not 200 <= status < 300:
        raise DiandianError(f'点点请求失败（HTTP {status}）。')
    if not isinstance(out, dict):
        raise DiandianError('点点返回了空响应或无法识别的数据。')
    if code in (-101, -1, '-101', '-1'):
        known_code = '-101' if code in (-101, '-101') else '-1'
        raise DiandianError(f'点点拒绝请求（返回码 {known_code}），原因未明确；'
                            '本次不重试。可用 `xhs status` 检查登录状态。')
    if code not in (0, '0') or out.get('success') is False:
        raise DiandianError('点点返回失败，未取得预期结果。')
    return out


def make_caller(cookies, ua, deadline):
    """使用当前 Python 环境的 xhshow；不加载其他解释器的 site-packages。"""
    try:
        from xhshow import Xhshow
        signer = Xhshow()
    except (ImportError, OSError):
        raise DiandianError('当前 Python 无法加载 `xhshow` 或其依赖。'
                            '请使用能导入 `xhshow` 的 xhs CLI 环境中的 Python，'
                            '或在当前 Python 环境中安装该依赖。') from None

    cookie_header = '; '.join(f'{key}={value}' for key, value in cookies.items())
    last_call = None

    def request(base, path, method, params, body, until):
        nonlocal last_call
        if last_call is not None:
            gap_left = REQUEST_GAP - (time.monotonic() - last_call)
            if gap_left > 0:
                wait_within(gap_left, until)
        headers = [f'Cookie: {cookie_header}', f'User-Agent: {ua}',
                   'Referer: https://www.xiaohongshu.com/ai_chat',
                   'Origin: https://www.xiaohongshu.com']
        url = f'https://{base}{path}'
        payload = []
        try:
            if method == 'POST':
                signed = signer.sign_headers_post(path, cookies, payload=body)
                headers.append('Content-Type: application/json;charset=UTF-8')
                payload = ['--data-binary', json.dumps(body, ensure_ascii=False)]
            else:
                params = params or {}
                signed = signer.sign_headers_get(path, cookies, params=params)
                url += '?' + '&'.join(f'{key}={value}' for key, value in params.items())
            headers.extend(f'{key}: {value}' for key, value in signed.items())
        except Exception:
            raise DiandianError('点点请求签名失败；请检查 Python 环境和本地登录文件。') from None

        left = remaining(until)
        max_time = max(0.001, min(REQUEST_TIMEOUT, left))
        connect_time = min(CONNECT_TIMEOUT, max_time)
        # 忽略本地 .curlrc；从 stdin 读取头字段，避免凭据和签名出现在进程参数中。
        cmd = ['curl', '--disable', '--silent', '--connect-timeout', f'{connect_time:.3f}',
               '--max-time', f'{max_time:.3f}', '--write-out', '\n%{http_code}',
               '--header', '@-', *payload, url]
        last_call = time.monotonic()
        try:
            response = subprocess.run(cmd, input=('\n'.join(headers) + '\n').encode(),
                                      capture_output=True, timeout=min(left, max_time + 2))
        except subprocess.TimeoutExpired:
            raise RequestTimeout('点点网络请求超时。') from None
        except FileNotFoundError:
            raise DiandianError('未找到 `curl`；请先安装并确认它在 PATH 中可用。') from None
        except OSError:
            raise DiandianError('无法启动 `curl`。') from None
        remaining(until)
        raw, _, status = response.stdout.rpartition(b'\n')
        if status in (b'401', b'403', b'429', b'461', b'471'):
            return checked_response(int(status), raw)
        if response.returncode == 28:
            raise RequestTimeout('点点网络请求超时。')
        if response.returncode != 0:
            raise DiandianError('连接点点服务失败，请检查网络后再试。')
        if not re.fullmatch(rb'[1-5][0-9]{2}', status):
            raise DiandianError('点点未返回可识别的 HTTP 状态。')
        return checked_response(int(status), raw)

    def call(base, path, method='GET', params=None, body=None, *, until=None):
        until = min(deadline, until) if until is not None else deadline
        try:
            return request(base, path, method, params, body, until)
        except RequestTimeout:
            # 只重试一次只读轮询的网络超时，不重复发起提问，也不重试服务拒绝。
            if method != 'GET' or remaining(until) <= REQUEST_GAP + 1:
                raise
            say('点点轮询请求超时，将重试一次…')
            return request(base, path, method, params, body, until)

    return call


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('note_id')
    ap.add_argument('--outdir', default=None)
    ap.add_argument('--cookie', default=str(COOKIE_JSON))
    ap.add_argument('--prompt', default=DEFAULT_PROMPT)
    ap.add_argument('--title', default='小红书视频')
    ap.add_argument('--author', default='')
    ap.add_argument('--duration', default='')
    ap.add_argument('--meta', default=None,
                    help='L1 fetch.py 产出的 meta.json 路径，用于共享标题/作者，'
                         '保证点点稿和平台字幕落到同一个帖子文件夹')
    ap.add_argument('--archive-folder', help='有多个已验证目录时，指定用户选中的目录名')
    args = ap.parse_args()
    started = time.monotonic()
    try:
        run(args, started + REQUEST_BUDGET)
    except (DiandianError, ArchiveError, OSError) as e:
        raise SystemExit(f'  {e}（已用时 {time.monotonic() - started:.1f} 秒）') from None
    say(f'总用时 {time.monotonic() - started:.1f} 秒')


def as_object(value):
    return value if isinstance(value, dict) else {}


_LABEL = re.compile(r'(?:#{1,6}\s*)?(?:(?:完整|视频|口播|原文|纯文字|转录|ASR)\s*)*(?:逐字稿|转录原文)')
_INTRO = re.compile(r'(?:以下是|这是|下面是|根据你引用的).{0,100}(?:逐字稿|转录原文)'
                    r'(?:\s*[（(][^）)]{0,100}[）)])?\s*[:：]')
_NON_TRANSCRIPT = re.compile(
    r'^(?:#{1,6}\s*)?(?:这(?:段|条|个)视频(?:的内容)?(?:主要讲|总结|概述|的摘要)'
    r'|(?:以下是|下面是)(?:该|这(?:段|条|个))?视频的(?:内容)?(?:摘要|总结|概述)'
    r'|(?:视频摘要|内容摘要|核心要点|视频总结)\s*[:：\n]'
    r'|(?:抱歉[，,。！!]?\s*)?(?:我)?(?:暂时)?(?:无法|不能|没有办法)'
    r'(?:直接)?(?:访问|打开|播放|提取|转写|转录|获取|提供|识别)'
    r'|你可以(?:尝试)?使用.{0,30}(?:工具|转录|提取))')
_TRUNCATED = re.compile(r'(?:[（(【\[])?\s*(?:后续内容省略|剩余内容省略|内容已截断|未完待续|下文省略|逐字稿未完)\s*[）)】\]]?[。.]?\s*$')


def transcript_body(answer):
    """Strip only recognized wrappers. Do not paraphrase or remove repeats."""
    lines = answer.strip().splitlines()
    first_line = lines[0].strip().strip('*').strip() if lines else ''
    if (_LABEL.fullmatch(first_line.rstrip(':：').rstrip()) or _INTRO.fullmatch(first_line)):
        lines.pop(0)
        while lines and not lines[0].strip():
            lines.pop(0)
    if lines and lines[0].strip() in {'---', '***'}:
        # Only remove a closing rule at the boundary (or one followed by a
        # clear service note). Internal rules can separate real content.
        marker = lines.pop(0).strip()
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].strip() == marker:
                tail = '\n'.join(lines[i + 1:]).strip()
                if not tail or (tail.startswith(('注：', '注:')) and '\n' not in tail):
                    if _TRUNCATED.search(tail):
                        raise DiandianError('点点回答注明内容截断，未归档为逐字稿。')
                    lines = lines[:i]
                break
    body = '\n'.join(lines).strip()
    if not any(ch.isalnum() for ch in body):
        raise DiandianError('点点没有返回非空的逐字稿正文。')
    if _NON_TRANSCRIPT.search(body):
        raise DiandianError('点点返回的内容像摘要、拒答或工具推荐，未归档为逐字稿。')
    if _TRUNCATED.search(body):
        raise DiandianError('点点回答注明内容截断，未归档为逐字稿。')
    return body


def latest_answer(out):
    items = as_object(out.get('data')).get('items')
    if isinstance(items, list):
        for item in reversed(items):
            message = as_object(as_object(item).get('message'))
            data = as_object(message.get('simple_data'))
            text = as_object(data.get('base_info')).get('text')
            if isinstance(text, str) and text.strip():
                return text.strip()
    return None


def poll_transcript(call, cid, deadline):
    previous, stable, last_error = None, 0, None
    poll_started = time.monotonic()
    poll_deadline = min(deadline, poll_started + POLL_TIMEOUT)
    say('等待点点返回回答…')
    for _ in range(POLL_MAX):
        try:
            wait_within(POLL_INTERVAL, poll_deadline)
            out = call('so.xiaohongshu.com', '/api/sns/web/v1/dqa/history/detail',
                       params={'conversation_id': cid, 'source': 'web_ask'}, until=poll_deadline)
        except DeadlineExpired:
            break
        answer = latest_answer(out)
        if answer:
            try:
                body = transcript_body(answer)
            except DiandianError as e:
                last_error = e
            else:
                last_error = None
                stable = stable + 1 if answer == previous else 1
                previous = answer
                if stable >= STABLE_POLLS:
                    say(f'已等待 {time.monotonic() - poll_started:.1f} 秒，回答文本连续两次一致。')
                    return body
                say('收到候选回答，继续确认文本是否还在变化…')
                continue
        previous, stable = None, 0
        say('暂未取得可用回答…')
    if last_error is not None:
        raise last_error
    raise DiandianError('未在轮询时限内取得稳定的点点回答，未归档为逐字稿。')


def run(args, deadline):
    # 只提取 24 位 note_id，不把带查询参数的链接作为引用值发送。
    m_id = re.search(r'(?<![0-9a-f])[0-9a-f]{24}(?![0-9a-f])', args.note_id.strip())
    if not m_id:
        raise DiandianError('未找到笔记 ID，请提供完整小红书笔记链接或 24 位笔记 ID。')
    note_id = m_id.group()

    base_dir = Path(args.outdir) if args.outdir else Path.cwd()
    note_folder(base_dir, {'note_id': note_id}, args.archive_folder, create=False)

    # 同一笔记的元信息只用于命名与说明。
    if args.meta:
        try:
            m = json.loads(Path(args.meta).read_text(encoding='utf-8'))
            if not isinstance(m, dict) or m.get('note_id') != note_id:
                raise ValueError
            if isinstance(m.get('title'), str) and m['title']:
                args.title = m['title']
            if isinstance(m.get('author'), str) and m['author']:
                args.author = m['author']
            if m.get('duration_sec'):
                args.duration = m['duration_sec']
        except (OSError, ValueError, RecursionError):
            say('读取 meta.json 失败，改用接口元信息。')

    cookies = load_cookies(args.cookie)
    ua = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 '
          '(KHTML, like Gecko) Chrome/157.0.0.0 Safari/537.36')
    call = make_caller(cookies, ua, deadline)

    # ---- 步骤 1: 注册引用笔记 ----
    say('注册引用笔记…')
    r1 = call('edith.xiaohongshu.com', '/api/sns/agentspark/web/note/detail',
              method='POST', body={'note_ids': [note_id]})
    details = as_object(r1.get('data')).get('content_detail_list')
    # 注意：note/detail 返回的 content_info.user 里只有 images（头像），
    # 没有 nickname，别指望从这取作者名——作者名应优先靠 --meta 复用 L1 的 meta.json。
    if isinstance(details, list) and details and not args.author:
        try:
            info = as_object(json.loads(as_object(details[0]).get('content_info', '{}')))
            if isinstance(info.get('title'), str) and info['title']:
                args.title = info['title']
            # info['user'] 无昵称字段，跳过；作者名交给 --meta 或口播 @昵称 兜底
        except (TypeError, ValueError, RecursionError):
            pass

    # ---- 步骤 2: 带引用发问 ----
    say('发起点点提问…')
    uid = str(uuidlib.uuid4())
    # 复用同一会话（点击点点的已有对话也可以；新会话让服务端自己建也可以——
    # 实测 send/ai 用已有 appCid 会追加消息，新 appCid 需浏览器端创建，
    # 这里直接用固定会话 ID 格式生成新的，服务端会自动建）
    cid = str(uuidlib.uuid4())
    body = {
        'uuid': uid, 'appCid': cid, 'domain': 'red',
        'bizType': 'dqa_chatsearch',
        'contentInfo': {'contentType': 101,
            'content': {'type': 1,
                        'data': json.dumps({'content': args.prompt}, ensure_ascii=False),
                        'degrade': '', 'summary': args.prompt[:50]}},
        'subBizType': 'davinci',
        'extension': {'source': 'web_ask', 'uuid': uid,
                      'multi_param_query': json.dumps(
                          [{'type': 'note', 'id': note_id}], ensure_ascii=False)},
    }
    r2 = call('so.xiaohongshu.com', '/api/sns/web/search/send/ai',
              method='POST', body=body)
    if not as_object(r2.get('data')).get('msgId'):
        raise DiandianError('点点未确认已接收提问，未开始轮询。')

    body_text = poll_transcript(call, cid, deadline)
    paras = [line.strip() for line in body_text.splitlines() if line.strip()]

    # ---- 步骤 5: 写交付文件，保留已有版本 ----
    outdir = note_folder(base_dir, {'title': args.title, 'author': args.author,
                                    'note_id': note_id}, args.archive_folder)
    meta = [f'# {args.title} — 点点逐字稿（纯文字）\n']
    meta.append(f'- 原笔记：{note_id}')
    if args.author:
        meta.append(f'- 作者：{args.author}')
    meta.append('- 来源：小红书「点点」返回的机器稿（未与视频原音核对，不能保证完整）')
    meta.append(f'- 会话 ID：{cid}')
    meta.append('')
    md = '\n'.join(meta) + '## 正文\n\n' + '\n\n'.join(paras) + '\n'
    out_name = deliver_name('点点逐字稿_纯文字', 'md')
    out, reused = save_version(outdir, out_name, md.encode('utf-8'))

    print(f'\n共 {len(paras)} 段 / {len(body_text)} 字；未与原音核对。')
    print(f'{"已复用" if reused else "已保存"} {out}')


if __name__ == '__main__':
    main()
