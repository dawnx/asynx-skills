---
name: asynx-image
description: 通过 Asynx 生成和编辑图片、生成视频，支持参考素材、批量任务、进度查询、中断恢复及本地图片处理。
---

# Asynx 图片与视频

使用本 Skill 的 `scripts/asynx.py`。将 `<skill-dir>` 替换为当前 Skill 的绝对路径；需要 Python 3.10+，Windows 可用 `py` 替代 `python3`。普通生成、换色和换背景按本页直接调用，其他用法按需读取参考文档。

## 生成与编辑

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<文字描述>"
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<文字描述>" --reference "/absolute/path/reference.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --image "/absolute/path/input.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --from-task "<task-id>"
```

- 新图用 `generate`，修改已有图片用 `edit`。参考图通过 `--reference` 或 `--image` 传入，多张图片重复参数；文件路径不作为 Prompt。
- 保留用户指定的主体、风格、构图和文字。明确的 Prompt 直接使用；用户要求优化或创作关系复杂时，再整理提示词。
- 未指定模型、尺寸或质量时省略对应参数；指定模型时直接传 `--model`，无需预先查询模型目录。
- 新图按用户指定的比例或用途传 `--aspect-ratio`，其余使用默认值。本地编辑默认保持原图比例，改变画幅时显式传参；URL 或多图比例冲突时，按脚本提示明确输出比例。
- 直接传原图，由脚本处理输入。保留原始编码、Mask 等特殊要求见[输入说明](references/operations.md#输入图片)。

单任务默认等待完成并下载。终端首次调用优先等待 30 秒（支持时设置 `yield_time_ms: 30000`）；进程仍在运行时，继续等待同一会话，每次 30–60 秒，以宿主限制为准。脚本负责轮询和下载，无需另起查询。

用户要求后台运行或稍后查询时加 `--detach`，告知任务已提交，之后通过原任务查询或等待。批量生成、追加与暂停见[批次操作](references/operations.md#批次)。

## 视频生成

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --prompt "<视频描述>"
python3 "<skill-dir>/scripts/asynx.py" video generate --first-frame "/absolute/path/input.png" --prompt "<动作与镜头要求>"
```

未指定参数时使用脚本默认值。用户指定的时长、分辨率和比例分别通过 `--duration`、`--resolution`、`--aspect-ratio` 原样传入。对白、音效和配乐要求写入 Prompt，无需声音开关。首尾帧、多参考素材和视频批次见[视频操作](references/video.md)。

## 选择素材与查询任务

“刚才那张”优先使用当前对话中已明确的文件或任务。需要查找时：

```bash
python3 "<skill-dir>/scripts/asynx.py" recent --query "<描述或任务 ID>"
python3 "<skill-dir>/scripts/asynx.py" recent --latest
python3 "<skill-dir>/scripts/asynx.py" asset list "<task-id>"
```

最近记录可能来自其他对话，使用前核对描述和素材类型，有歧义再询问。图片编辑的 `--from-task` 会使用该任务的全部本地图片；只选一张时传 `--image`。视频的 `--from-task` 只接受单张图片作为首帧。

```bash
python3 "<skill-dir>/scripts/asynx.py" task list
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
```

查询进度用 `task status --refresh`，等待并下载用 `wait`，均接受本地或远端任务 ID。提交中断的恢复和取消见[任务恢复](references/operations.md#任务恢复与取消)；查询进度不执行恢复操作。

## 按需读取

只读取当前请求需要的参考：

| 请求 | 参考 |
| --- | --- |
| 视频首尾帧、多参考素材、复用图片或视频批次 | [视频操作](references/video.md) |
| 批量生成、追加、暂停或继续批次 | [批次操作](references/operations.md#批次) |
| 缩放、裁剪、切图、格式转换或应用已有 Mask | [本地图片工具](references/operations.md#本地图片工具)，不需要 API Key |
| 输入图片限制、Mask 编辑或特殊参数 | [输入与参数](references/operations.md#输入图片) |
| 恢复中断、取消任务、找回下载文件 | [任务恢复](references/operations.md#任务恢复与取消) |
| 配置、连接异常、安装、升级或卸载 | [配置与维护](references/setup.md) |
| 模型推荐、Prompt 优化、多图关系或复杂图文布局 | [模型与提示词](references/model-prompting.md) |

缺少 API Key 时，请用户在自己的终端运行以下命令并输入密钥；已配置却报错时，按配置参考排查。

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
```

## 交付结果

- 成功后直接交付 `files` 中的文件。宿主支持本地预览时展示图片或视频，否则提供文件链接；路径使用绝对路径。类型和图片尺寸等信息使用结果中的 `media`、`images`、`videos`，无需重复查询。
- 默认不追加费用查询或视觉评审。用户要求检查、纠错时，针对指定问题查看；文件下载成功不代表创作要求已满足，重新生成需符合用户授权。
- 用自然语言说明进度、已知错误及下一步，不直接转发 JSON、诊断字段或上游英文，不猜测原因。部分失败、下载失败和 `degraded` 必须说明；未提供降级原因时，不断言画质变差。
- 中断等待不等于取消，已开始的任务可能继续执行和计费。优先继续原任务或重试原下载，不用重新生成代替恢复，也不自动重试失败的付费生成。返回 `next_action_args` 时，可用其继续原任务。
- 有警告、更新或旧版安装提示时，先交付结果，再说明相关事项。无需在生成前检查更新；升级和清理须由用户提出。

API Key 只通过本机配置或环境变量提供，不写入对话、命令参数、Prompt、日志、项目文件或浏览器代码。任务和素材通过脚本管理，不直接操作或依赖 Skill 的数据库与内部状态文件。用户要求接入自己的工作流或工作台时，可调用公开 API 或封装脚本。
