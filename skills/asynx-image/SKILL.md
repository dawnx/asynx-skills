---
name: asynx-image
description: 通过 Asynx 生成、参考图创作和编辑图片，管理批量任务、进度与中断恢复；也支持本地缩放、裁剪、切图和格式转换。用户要求时可接入自定义工作流，不用于视频。
---

# Asynx 图片

使用本 Skill 的 `scripts/asynx.py` 完成图片操作。下文 `<skill-dir>` 替换为当前 Skill 的绝对路径；需要 Python 3.10+，Windows 可用 `py` 替代 `python3`。普通请求直接执行，不先检查版本、扫描目录或读取全部参考文件。

## 生成与编辑

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<文字描述>"
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<文字描述>" --reference "/absolute/path/reference.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --image "/absolute/path/input.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<修改要求>" --from-task "<task-id>"
```

- 新图使用 `generate`，修改已有图片使用 `edit`。图片通过 `--reference` 或 `--image` 独立传入，不把路径当作 Prompt；多张输入重复对应参数。
- 用户给定的 Prompt 保留原意，普通请求直接提交；只有需要整理复杂创作意图或用户要求优化时才补充结构，不擅自改变主体、风格、构图和文字。
- 未指定模型时省略 `--model`，使用脚本固定默认模型；完整模型名直接传入，模糊名称由脚本匹配。不要自行切换模型或为普通请求先查 `models`。
- 比例以用户要求为先；海报、壁纸、横幅等用途明确时传合适的 `--aspect-ratio`，其余使用脚本默认值。尺寸和质量默认不传；指定质量时使用模型支持的值，不传 GPT Image 不支持的 `standard`。
- 本地参考图交给脚本压缩和校验，不额外预处理；只有用户要求保留原始编码时用 `--keep-reference-original`。Mask 或输入限制见[操作参考](references/operations.md#输入图片)。
- 单任务默认等待、下载并展示结果。用户要求后台运行或稍后查询时才加 `--detach`；它只表示已受理。若先分离提交但用户仍要求本轮完成，继续等待原任务，不重新生成。
- 批量或追加使用批次命令，按下面的按需入口读取批次说明；不要套用单任务的等待规则。

## 选择图片与查询任务

“刚才那张”优先对应当前对话中已明确的文件或任务。上下文不足时查询本地索引，不扫描磁盘或用远端历史猜测：

```bash
python3 "<skill-dir>/scripts/asynx.py" recent --query "<描述或任务 ID>"
python3 "<skill-dir>/scripts/asynx.py" recent --latest
python3 "<skill-dir>/scripts/asynx.py" asset list "<task-id>"
```

`recent --latest` 是同一用户共享记录中的最近结果，不保证属于当前会话；核对描述后再使用，有歧义才询问。一个任务有多张结果时，`--from-task` 会使用该任务的多张本地结果；用户只选一张时，传该图片的 `--image` 路径。

```bash
python3 "<skill-dir>/scripts/asynx.py" task list
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task poll "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" status "<remote-task-id>"
python3 "<skill-dir>/scripts/asynx.py" wait "<remote-task-id>"
```

`task status` 是本地快照；`task poll` 刷新指定任务并下载完成结果；顶层 `status` 只查询远端，不更新本地记录；`wait` 持续等待远端任务。查询进度不要使用 `task recover`：它可能重新提交中断的请求，且不能指定单个任务。恢复、取消和文件缺失的处理见[任务恢复](references/operations.md#任务恢复与取消)。

## 按需读取

只读与当前请求有关的章节，普通生图无需读取：

| 请求 | 参考 |
| --- | --- |
| 批量生成、追加、暂停或继续批次 | [批次操作](references/operations.md#批次) |
| 缩放、裁剪、切图、格式转换或应用已有 Mask | [本地图片工具](references/operations.md#本地图片工具)，不需要 API Key |
| 输入图片限制、Mask 编辑或特殊参数 | [输入与参数](references/operations.md#输入图片) |
| 恢复中断、取消任务、找回下载文件 | [任务恢复](references/operations.md#任务恢复与取消) |
| 配置、连接异常、安装、升级或旧入口清理 | [配置与维护](references/setup.md) |
| 模型推荐、Prompt 优化、复杂编辑或图文布局 | [模型与提示词](references/model-prompting.md)，读取本身不要求查模型目录 |

缺少 API Key 时，让用户在自己的终端运行以下命令，不在对话中索取密钥；已配置却报错时按维护参考诊断：

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
```

## 结果与边界

- 成功优先展示图片或文件，Codex 桌面端用绝对路径展示。默认不罗列任务 ID、模型、计费字段或日志；用户查询时再提供。
- 进度用自然语言概括；失败说明已知原因、影响和下一步，不直接转发 JSON 或上游英文，也不猜测原因。部分失败、下载失败和 `degraded` 等影响结果的信息必须说明；降级原因未提供时，不自行断言画质变差。
- 中断等待不等于取消。已开始的任务可能继续执行和计费；恢复结果不能通过重新生图替代，不无条件重试失败任务。
- 返回 `warnings`、`update_notice` 或 `legacy_installation_notice` 时，先交付本次结果，再说明相关提示。不要提前查更新；未收到升级或清理指令时不替换、移动文件。
- API Key 不得进入对话、命令参数、Prompt、日志、项目文件或浏览器代码。通过脚本管理任务，不直接读取、修改或依赖本 Skill 的数据库及内部状态文件。

用户要求自定义工作台或工作流时，可以调用公开 API 或封装本脚本，使用自己的数据库；遵守密钥和本地状态边界即可，不额外限定技术栈或引入用户未要求的服务。
