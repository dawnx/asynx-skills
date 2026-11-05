---
name: asynx-image
description: 通过 Asynx 生成和编辑图片、生成视频，支持参考素材、批量任务、进度查询、中断恢复及本地图片处理。
---

# Asynx 图片与视频

使用本 Skill 的 `scripts/asynx.py`。请将 `<skill-dir>` 替换为当前 Skill 的绝对路径；需要 Python 3.10+，Windows 可使用 `py` 替代 `python3`。普通生成、换色与换背景按本页直接调用，其他用法按需读取参考文档。

## 生成与编辑

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<文字描述>"
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<文字描述>" --reference "/absolute/path/reference.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --image "/absolute/path/input.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --from-task "<task-id>"
```

- 新图使用 `generate`，修改已有图片使用 `edit`。参考图通过 `--reference` 或 `--image` 传入，多张时重复该参数；文件路径不得写入提示词。
- 保留用户指定的主体、风格、构图与文字。用户已写明的提示词直接使用；仅在用户要求优化或创作关系复杂时整理提示词。
- 未指定质量时不发送该字段，由渠道或上游使用默认值。未指定模型、尺寸时省略对应命令选项；指定模型时直接传入 `--model`，无需预先查询模型目录。
- 新图按用户指定的画幅比例或用途传入 `--aspect-ratio`，其余情况使用默认值。本地编辑默认保持原图比例，需改变画幅时显式传入该参数；输入为 URL 或多张图片比例不一致时，按脚本提示明确输出比例。
- 常用画幅仅为建议，不保证每个模型都支持；参数被拒绝时说明已知原因，不自动改参数重新生成。GIF/SVG 结果保留原文件，不直接作为后续编辑输入。
- 原图直接传入，由脚本处理输入。需保留原始编码或使用蒙版等特殊要求见[输入说明](references/operations.md#输入图片)。

单任务默认等待完成并下载。终端首次调用优先等待 30 秒（宿主支持时设置 `yield_time_ms: 30000`）；进程仍在运行时继续等待同一会话，每次 30–60 秒，以宿主限制为准。轮询与下载由脚本负责，无需另起查询命令。

用户要求后台运行或稍后查询时使用 `--detach`，说明任务已提交，之后通过原任务查询或等待。批量生成、追加与暂停见[批次操作](references/operations.md#批次)。

## 视频生成

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --prompt "<视频描述>"
python3 "<skill-dir>/scripts/asynx.py" video generate --first-frame "/absolute/path/input.png" --prompt "<动作与镜头要求>"
```

未指定参数时使用脚本默认值。用户指定的时长、分辨率与画幅比例分别通过 `--duration`、`--resolution`、`--aspect-ratio` 原样传入。对白、音效与配乐要求写入提示词，无独立声音开关。首尾帧、多参考素材与视频批次见[视频操作](references/video.md)。

## 选择素材与查询任务

“刚才那张”优先使用当前对话中已明确指出的文件或任务。需要查找时使用：

```bash
python3 "<skill-dir>/scripts/asynx.py" recent --query "<描述或任务 ID>"
python3 "<skill-dir>/scripts/asynx.py" recent --latest
python3 "<skill-dir>/scripts/asynx.py" asset list "<task-id>"
```

最近记录可能来自其他对话，使用前须核对描述与素材类型，存在歧义时询问用户。图片编辑的 `--from-task` 使用该任务的全部本地图片；仅需一张时传入 `--image`。视频的 `--from-task` 仅接受单张图片作为首帧。

```bash
python3 "<skill-dir>/scripts/asynx.py" task list
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
```

查询进度使用 `task status --refresh`，等待并下载使用 `wait`，两者均接受本地任务 ID 或服务端任务 ID。提交中断后的恢复与取消见[任务恢复](references/operations.md#任务恢复与取消)；仅查询进度时不得执行恢复操作。

## 按需读取

仅读取当前请求所需的参考文档：

| 请求 | 参考 |
| --- | --- |
| 视频首尾帧、多参考素材、复用图片或视频批次 | [视频操作](references/video.md) |
| 批量生成、追加、暂停或继续批次 | [批次操作](references/operations.md#批次) |
| 缩放、裁剪、切图、格式转换或应用已有蒙版 | [本地图片工具](references/operations.md#本地图片工具)，不需要 API Key |
| 输入图片限制、蒙版编辑或特殊参数 | [输入与参数](references/operations.md#输入图片) |
| 恢复中断、取消任务、补下载结果文件 | [任务恢复](references/operations.md#任务恢复与取消) |
| 配置、连接异常、安装、升级或卸载 | [配置与维护](references/setup.md) |
| 模型推荐、提示词优化、多图关系或复杂图文排版 | [模型与提示词](references/model-prompting.md) |

缺少 API Key 时，请用户在自己的终端运行下列命令并输入密钥；已配置但仍报错时，按配置参考排查。

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
```

## 交付结果

- 成功后直接交付 `files` 中的文件。宿主支持本地预览时展示图片或视频，否则提供文件链接；路径使用绝对路径。类型与图片尺寸等信息直接取自结果中的 `media`、`images`、`videos`，无需重复查询。
- 默认不额外查询费用，也不进行视觉评审。用户要求检查或纠错时，仅针对其指出的问题查看；文件下载成功不代表创作要求已满足，重新生成须经用户同意。
- 用自然语言说明进度、已知错误与下一步，不得直接向用户输出 JSON、诊断字段或上游英文，也不得猜测原因。部分失败、下载失败与 `degraded` 必须说明；未提供降级原因时，不得断言画质下降。
- 等待中断不等于取消，已开始的任务可能继续执行并计费。应优先继续原任务或重试原下载，不得以重新生成代替恢复，也不得自动重试失败的付费生成。返回 `next_action_args` 时，可用其继续处理原任务。
- 出现警告、更新或旧版安装提示时，先交付结果，再说明相关事项。无需在生成前检查更新；升级与清理须由用户提出。

API Key 仅通过本机配置或环境变量提供，不得写入对话、命令参数、提示词、日志、项目文件或浏览器代码。任务与素材通过脚本管理，不得直接操作或依赖 Skill 的数据库与内部状态文件。用户需要接入自有工作流或工作台时，可调用公开 API 或封装脚本。
