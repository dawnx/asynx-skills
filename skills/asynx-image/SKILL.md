---
name: asynx-image
description: 通过 Asynx 生成和编辑图片、制作 SVG 矢量图与 GIF 动画、生成视频，由文字或图片生成 3D 模型（GLB）并为其绑定骨骼、套用动画、转换格式，支持参考素材、批量任务、进度查询、中断恢复及本地图片处理。
---

# Asynx 图像创作

使用本 Skill 的 `scripts/asynx.py`，`<skill-dir>` 为本文件所在目录的绝对路径。需要 Python 3.10+；Windows 可用 `py` 替代 `python3`。普通请求直接调用，特殊操作按需读取参考文档。

## 生成与编辑

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<描述>"
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<描述>" --reference "/absolute/path/reference.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --image "/absolute/path/input.png"
```

命令与模型：

- 新图用 `generate`，修改原图用 `edit`。图片通过参数传入，多张时重复参数，路径不写入提示词；用户的提示词与明确参数原样保留。
- 未指定模型时省略 `--model`，由服务按任务类型和输出格式选用默认模型，返回结果中的 `model` 是实际使用的模型。用户指定的模型原样传入。需要知道当前默认模型时查询 `models`，其中 `default_for` 标明各默认模型对应的请求。
- 默认模型的普通静态单图直接提交，不预查目录或说明。仅在以下情况读取模型说明：用户指定了当前对话尚未确认过的模型（`models describe --model "<模型名>"`）；需要透明背景、组图、Mask/Markup、SVG/GIF 或新的高级参数，而当前对话没有对应能力信息；收到能力拒绝之后。名称有歧义或需要探索、推荐时才查询 `models`。说明的内容、复用范围与 `unconfirmed` 的含义见[读取模型说明](references/model-prompting.md#读取当前模型说明)。

尺寸与画幅：

- 明确像素用 `--size 宽x高`，与 `--image-size`、`--aspect-ratio` 互斥；档位（1K、2K、4K）与画幅可组合。不自行换算像素，未指定档位时省略 `--image-size`。
- 新建静态图未指定像素或画幅时，按用途选比例：封面、风景、横版展示用 `16:9`；海报、角色立绘用 `2:3`；手机壁纸用 `9:16`；图标、头像或明确方形需求用 `1:1`；用途不明确时用 `3:2`。这是构图建议，不保证所有模型都支持，也不因此提高分辨率档位。
- 编辑本地静态图默认保持原图比例。GIF 未指定任何尺寸时由脚本发送 `size=auto`，不按用途补比例，也不继承原图比例。

格式、质量与数量：

- SVG/GIF 使用 `--output-format svg` 或 `--output-format gif`，参数与限制见[高级图片参数](references/operations.md#高级图片参数与结果格式)。
- 未指定时省略 `--quality` 与 `--complexity`，由服务按所用模型采用默认值；脚本不补任何值。
- `--count` 是一个任务的输出张数，`batch --total` 是独立任务数。单图模型需要多张时，在用户已授权的数量与费用范围内改用批次；不承诺跨任务的角色、构图或连接点严格一致。

费用与授权：

- 参数被拒绝时不会创建任务。向用户说明原因，由用户决定如何调整后再提交。
- 用户想先了解费用，或批次数量较大时，加 `--quote`：只返回服务报价，不提交任务、不扣费。
- 用户明确要求比较模型或测试参数时，可在约定的数量、预算及候选范围内连续执行，无需逐项再次确认；单项失败后记录原因，继续其他已授权组合，不重复提交同一失败请求。结论仅覆盖本次实测，无法确认的失败原因不归咎于模型。

命令默认等待任务完成并下载；受理时会在 stderr 打印任务 ID 和继续等待的命令。命令工具提前返回运行中状态时，继续等待同一进程，可设置等待时长时每次约 30–60 秒。宿主对单条命令有时长上限时，加 `--max-wait <秒>`（略小于上限）：到时返回 `wait_timeout: true` 与仍在运行的任务，任务不会被取消。命令被中断或已返回运行中时，按返回的 `next_action_args` 执行 `wait` 取回同一个任务。用户要求先提交时才用 `--detach`，按实际返回状态说明进度。

## 素材与任务

“刚才那张”优先使用当前对话中的文件或任务；上下文不足时查询：

```bash
python3 "<skill-dir>/scripts/asynx.py" recent --query "<描述或任务 ID>"
python3 "<skill-dir>/scripts/asynx.py" asset list "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task list
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
```

最近记录跨会话共享，使用前核对。`edit --from-task` 使用该任务的全部本地图片；只选一张时传 `--image`。查询用 `task status --refresh`，等待和补下载用 `wait`；均接受本地或服务端任务 ID，只读取已有任务。

出错时，返回的数据已说明如何处理：

- `error.message` 是服务给用户的说明，直接转述即可。
- `error.next_step` 说明下一步：`change_request` 需调整参数、提示词或素材；`fix_account` 由用户处理账户、余额或密钥；`retry_later` 可稍后原样重试；`contact_support` 需联系服务支持，重试无效。任务失败时的 `error.next_step` 含义相同。
- 提交出错且带 `task_created: false`：服务确认未创建任务、未扣费，按 `next_step` 处理后可以重新提交。
- 提交出错而没有 `task_created`：任务可能已受理。执行返回的 `next_action_args`（`task recover <id>`）确认。`task recover` 只向服务查询，不会提交或扣费：查到原任务就继续等待和下载；确认没有创建时返回 `task_created: false`；返回 `submission_pending` 表示原请求可能仍在途中，稍后再确认。
- 确认之前，相同的请求不会被再次发送：命令以 `unconfirmed_submission_exists` 停止，并给出同一条确认命令。

## 按需参考

| 需求 | 文档 |
| --- | --- |
| 批量、按清单生成、报价、追加、暂停或继续 | [批次](references/operations.md#批次) |
| 提交中断、取消、补下载 | [任务恢复与取消](references/operations.md#任务恢复与取消) |
| 缩放、裁剪、切图、格式转换 | [本地图片工具](references/operations.md#本地图片工具) |
| 参考图限制、蒙版 | [输入图片](references/operations.md#输入图片) |
| SVG、GIF、透明背景、标注或高级参数 | [高级图片参数](references/operations.md#高级图片参数与结果格式) |
| 首次设置、配置、默认保存目录、清理任务记录、安装与更新渠道或排障 | [配置与维护](references/setup.md) |
| 模型探索、推荐、提示词优化或复杂构图 | [模型与提示词](references/model-prompting.md) |
| 视频 | [视频操作](references/video.md) |
| 3D 模型（文字或图片生成 GLB，绑定检查、骨骼绑定、动画、格式转换） | [3D 模型操作](references/model3d.md) |

## 交付与边界

- 成功后直接交付 `files`，用绝对路径预览或提供链接；尺寸、格式等使用返回元数据。普通交付不额外查费用或评审画面，用户要求检查或排障时再展开。
- 用自然语言说明已知错误、部分失败、下载失败和降级标记；没有原因不推断画质下降。用户明确要求接口结果时可提供脱敏 JSON。
- 中断等待不等于取消，任务仍在服务端继续。用户明确要新结果时才提交新任务。
- 结果附带警告时一并说明。命令报告缺少依赖（`pillow_not_installed`）或密钥（`missing_api_key`）时运行 `setup`：它会装好依赖，并返回 `configure_command`，由用户在自己的终端运行以输入密钥。

以下两点命令无法替你把关：

- 密钥只存在于用户的终端和配置文件中，不出现在对话、命令参数、提示词、日志、项目文件或浏览器代码里。
- 任务与批次的状态只通过命令读写，不直接修改 Skill 的数据库和缓存文件。
