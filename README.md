# 小红书视频逐字稿 · Lite

一个面向内容研究和产品调研的 Agent Skill：把有权访问的小红书视频笔记整理成可回查的 Markdown 字幕或机器稿。它不下载视频，也不运行本地语音识别。

## 为什么做这个工具

调研时，视频里的原话经常要反复拖动进度条才能核对；直接用模型总结虽然快，却容易丢掉措辞和上下文。这个 Skill 把**机器原稿、审校和解读**分开：有时间轴时能回到原视频核对，只有纯文字时也不假装知道时间点。

| 路径 | 输出 | 边界 |
| --- | --- | --- |
| 平台字幕 | 保留时间轴的原始机器字幕 | 并非每条视频都有字幕轨 |
| 小红书「点点」 | 单独存放的纯文字机器稿 | 实验性路径，取决于平台接口；可能漏句或重复，未与原音核对 |
| 按需审校或解读 | 基于已有底稿的派生内容 | 不改写原稿、不猜时间戳，不把解读当成作者原话 |

默认先取平台字幕，再尝试点点；用户指定一种来源时不会擅自切换。同一笔记按完整 ID 归档：相同正文复用已有文件，正文变化另存版本，不覆盖旧稿。

## 先试一下（离线，不需要账号）

环境：Python 3.10+。在仓库根目录运行：

```bash
python3 -B -m unittest discover -s tests -v
```

测试覆盖毫秒级时间戳解析、短口播和原话重复保留、明显摘要或截断拒绝归档、审校定位冲突以及归档版本保留。测试使用合成输入，不验证线上接口可用性或转录准确率。

## 联网使用

在支持 Agent Skills 的工具中加载本仓库的 [SKILL.md](SKILL.md)，然后提供一条有权访问的视频笔记链接。也可以在终端使用入口脚本：

- 安装第三方 `xiaohongshu-cli`，确认 `xhs` 在 `PATH` 中，并自行运行 `xhs login` 或 `xhs login --qrcode`。不要分享 Cookie、验证码或包含访问令牌的链接。
- 点点路径另需 `curl` 和安装 `xhs` 的 Python 环境中的 `xhshow`。

以下两条路径可单独执行。请将 `NOTE` 替换为你自己的视频笔记链接：

```bash
NOTE='<完整小红书视频笔记链接>'
WORKDIR=$(mktemp -d)
DEST='./output'

# 平台已有字幕：下载字幕、导出 Markdown、归档
python3 scripts/run_extract.py platform "$NOTE" "$WORKDIR" &&
python3 scripts/export_subtitle.py "$WORKDIR" --source platform &&
python3 scripts/collect.py "$WORKDIR" --dest "$DEST"

# 点点机器稿：直接归档到同一交付目录
python3 scripts/run_extract.py diandian "$NOTE" "$DEST"
```

审校与解读见[语义审校流程](references/semantic-review.md)和[视频解读流程](references/video-explanation.md)。本项目与小红书官方无关。请遵守平台规则、作者版权和隐私要求；遇到验证码、访问限制或接口变更时，不尝试绕过。
