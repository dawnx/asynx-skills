---
name: asynx-image
description: 通过 Asynx 生成和编辑图片、制作 SVG 矢量图与 GIF 动画，支持参考素材、批量任务、进度查询、中断恢复及本地图片处理。
---

# Asynx 图像创作

使用本 Skill 的 `scripts/asynx.py`，`<skill-dir>` 为本文件所在目录的绝对路径。需要 Python 3.10+；Windows 可用 `py` 替代 `python3`。普通请求直接调用，特殊操作按需读取参考文档。

## 生成与编辑

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<描述>"
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<描述>" --reference "/absolute/path/reference.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --image "/absolute/path/input.png"
```

- 保留用户提示词和明确参数。新图用 `generate`，修改原图用 `edit`；图片通过参数传入，多张时重复参数，路径不写入提示词。
- 未指定模型时省略 `--model`：普通图片默认 `gpt-image-2.5-sunburst`，SVG/GIF 默认 `gpt-image-2.6-sunburst`。指定模型时直接传入，无需预查目录。
- SVG/GIF 使用 `--output-format svg` 或 `--output-format gif`；SVG 编写模型默认 `gpt-6.1-sol`，无需额外填写。输入格式见[输入图片](references/operations.md#输入图片)。
- GIF 未指定任何尺寸时，脚本发送 `size=auto`，不按静态图用途补比例，也不继承编辑原图比例；用户明确填写的像素、档位或比例保留。所选协议是否支持GIF及参数组合由服务判断。帧数与尺寸共同受上游像素预算限制，详见[GIF 参数](references/operations.md#gif-动画)。
- 明确像素用 `--size 宽x高`，与 `--image-size`、`--aspect-ratio` 互斥；档位（1K、2K、4K）与画幅可组合。不自行换算像素，未指定档位时省略 `--image-size`；静态图本地编辑默认保持原图比例。
- 新建静态图未指定像素或画幅时，按用途选比例：封面、风景、横版展示用 `16:9`；海报、角色立绘用 `2:3`；手机壁纸用 `9:16`；图标、头像或明确方形需求用 `1:1`。用途不明确时倾向 `3:2`，不额外提高分辨率档位。这些是构图建议，不保证所有模型都支持。
- 未指定质量或规划复杂度时省略 `--quality`、`--complexity`，由渠道或上游使用默认值。原图直接交给脚本处理；参数被拒绝时说明原因，不猜值或自动重提。
- 用户明确要求比较模型或测试参数时，可在约定的数量、预算及候选范围内连续执行，无需逐项再次确认；单项失败后记录原因，继续其他已授权组合，不重复提交同一失败请求。结论仅覆盖本次实测，无法确认的失败原因不归咎于模型。

命令默认等待任务完成并下载。命令工具提前返回运行中状态时，继续等待同一进程，可设置等待时长时每次约 30–60 秒。无法继续原进程时，按返回的任务 ID 执行 `wait`，不另起 `generate` 或 `edit`。用户要求先提交时才用 `--detach`，按实际返回状态说明进度。

## 素材与任务

“刚才那张”优先使用当前对话中的文件或任务；上下文不足时查询：

```bash
python3 "<skill-dir>/scripts/asynx.py" recent --query "<描述或任务 ID>"
python3 "<skill-dir>/scripts/asynx.py" asset list "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task list
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
```

最近记录跨会话共享，使用前核对。`edit --from-task` 使用该任务的全部本地图片；只选一张时传 `--image`。查询用 `task status --refresh`，等待和补下载用 `wait`；均接受本地或服务端任务 ID，不重新生成。

## 按需参考

| 需求 | 文档 |
| --- | --- |
| 批量、追加、暂停或继续 | [批次](references/operations.md#批次) |
| 提交中断、取消、补下载 | [任务恢复与取消](references/operations.md#任务恢复与取消) |
| 缩放、裁剪、切图、格式转换 | [本地图片工具](references/operations.md#本地图片工具) |
| 参考图限制、蒙版 | [输入图片](references/operations.md#输入图片) |
| SVG、GIF、透明背景、标注或高级参数 | [高级图片参数](references/operations.md#高级图片参数与结果格式) |
| 安装、配置、升级或排障 | [配置与维护](references/setup.md) |
| 模型探索、推荐、提示词优化或复杂构图 | [模型与提示词](references/model-prompting.md) |
| 视频 | [视频操作](references/video.md) |

## 交付与边界

- 成功后直接交付 `files`，用绝对路径预览或提供链接；尺寸、格式等使用返回元数据。普通交付不额外查费用或评审画面，用户要求检查或排障时再展开。
- 用自然语言说明已知错误、部分失败、下载失败和降级标记；没有原因不推断画质下降。用户明确要求接口结果时可提供脱敏 JSON。
- 中断等待不等于取消。等待或下载已提交的任务不创建新生成；恢复未确认的提交按返回指引处理原请求。只有用户明确要新结果时才提交新任务。
- 结果附带警告或更新提示时一并说明，升级与旧入口清理由用户提出。缺密钥时请用户在自己的终端运行 `configure`，不要在对话中索取密钥。

密钥不得进入对话、命令参数、提示词、日志或项目文件。通过公开 API 或脚本接入工作流，不直接操作 Skill 的数据库与内部状态文件，也不把密钥放入浏览器代码。
