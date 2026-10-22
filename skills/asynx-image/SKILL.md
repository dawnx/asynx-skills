---
name: asynx-image
description: 通过已配置的 Asynx API 生成、编辑和批量处理图片。用于生图、参考图、图片编辑、批量任务、进度查询和中断恢复；用户明确要求时，也可接入自定义工作台或业务工作流。不要用于视频或非图片任务。
---

# Asynx 图片

普通对话请求使用本 skill 目录中的 `scripts/asynx.py` 完成 Asynx API 操作，不要绕过脚本自行拼接 curl。用户明确要求自定义集成时，可以在自己的后端或脚本中调用 Asynx 公开 API。脚本需要 Python 3.10 或更高版本。
API Key 不得出现在命令参数、Prompt、日志或项目文件中。

## 对话式使用

默认图片对话流程中，用户只需要用自然语言描述目标，不需要知道 CLI 参数、Task ID 或本地数据库。Agent 在后台选择并执行对应操作，默认等待完成并直接展示最终图片；不要把 shell 命令或中间 JSON 当作回复主体。用户明确要求构建工作台、页面、自动化或业务集成时，可以切换到 API/工作流模式。

- “生成/画一张……”：执行 `generate`，原样提交用户描述，完成后展示图片。
- “换个颜色/改背景/把刚才那张……”：先用 `recent --latest` 找最近结果，再执行 `edit --from-task`；只有结果有多个且无法判断时才追问。
- “再来 5 张/批量生成……”：执行 `batch create` 或 `batch add`，返回批次进度和可继续操作的状态。
- “继续上次/现在到哪了……”：执行 `task recover` 或 `task poll`，汇总状态、失败项和已下载图片。
- 用户没有明确要求后台运行时，即使先使用 `--detach` 快速受理，也必须在同一轮继续等待、下载和展示最终结果。
- 用户没有要求解释实现时，不要要求用户手动复制 Task ID、运行 `history` 或扫描磁盘。

## 路由规则

- 新图使用 `generate`；换色、替换背景、修改或局部重绘使用 `edit`。
- 参考/模仿图片必须作为独立的 `--reference`（生成）或 `--image`（编辑）参数，Prompt 只保留文字指令。
- 普通请求中 Prompt 原样提交；未指定模型时使用当前固定默认模型 `gpt-image-2.5-sunburst`，不随机切换模型；未指定尺寸、比例或质量时使用脚本默认值，不先调用 `models` 探测。
- 质量默认使用 `auto`，只有用户指定时才传 `--quality`；值需由所选模型支持。GPT Image 不使用 `standard`。
- 未指定比例时，普通主体、头像、图标和方形素材默认使用 `1:1`；海报、卡牌、手机壁纸、横幅、封面等有明显画幅用途时，根据用途选择比例；用途和构图都无法判断且比例影响较大时再询问用户。
- 用户要求模型推荐、Prompt 优化，或请求包含复杂编辑、多参考图、海报、信息图或组图时，读取
  [references/model-prompting.md](references/model-prompting.md)。其中的模型策略是经验建议，不是供应商硬性语法；保留用户原始意图，不要无理由重写 Prompt。
- 普通请求直接执行 `generate`/`edit` 并等待下载；用户要求立即返回、后台运行或稍后查询时加 `--detach`。
- 用户要求批量或追加时使用 `batch create`/`batch add`，不要用批次命令替代单 Task 操作。

完整规范的模型名直接提交；只有模糊模型选择才读取并缓存模型目录。输入图片使用绝对路径，也支持 HTTP(S) URL；Windows 路径作为完整参数引用，例如
`--reference "E:\\images\\source.png"`。Mask 只能在模型能力声明支持时传入。

常用画幅建议：头像、图标和方形产品图使用 `1:1`；卡牌立绘和竖版海报使用 `2:3` 或 `9:16`；社交媒体竖图使用 `3:4` 或 `9:16`；横版海报和视频封面使用 `16:9`；商品横图使用 `3:2` 或 `16:9`。

## 自定义集成

用户明确要求时，可以将 Asynx 接入自己的网页、工作台、脚本或业务工作流。用户可以自由选择直接调用公开 API、调用
`asynx.py`，或在自己的后端中封装；不限制技术栈、页面结构、数据库、轮询方式或业务流程。

必须遵守以下边界：

- 不要直接读取、写入、迁移、删除或依赖本 Skill 管理的本地状态文件。
- 任务状态通过 CLI 的结构化 JSON 输出或 Asynx 公开 API 获取；用户自己的工作台可以维护自己的数据库。
- API Key 不得暴露到浏览器代码、URL、前端构建产物或日志；浏览器直连时必须由用户自行提供安全的服务端或本地桥接方案。
- 不要因为用户要自定义页面，就替用户创建额外网关、服务或固定工作流；只实现用户明确要求的集成部分。

## 本地图片处理

用户只要求处理已有图片时，优先使用本地确定性工具；这些命令不调用 Asynx、不需要 API Key，也不读写 Skill 的任务记录：

```bash
python3 "<skill-dir>/scripts/asynx.py" image info input.png
python3 "<skill-dir>/scripts/asynx.py" image convert input.png --output output.webp --format webp
python3 "<skill-dir>/scripts/asynx.py" image resize input.png --output small.png --width 1200
python3 "<skill-dir>/scripts/asynx.py" image crop input.png --output crop.png --box 0,0,800,800
python3 "<skill-dir>/scripts/asynx.py" image slice input.png --output-dir tiles --rows 3 --columns 3
python3 "<skill-dir>/scripts/asynx.py" image contact-sheet images/*.png --output sheet.jpg
python3 "<skill-dir>/scripts/asynx.py" image apply-mask input.png --mask mask.png --output cutout.png
python3 "<skill-dir>/scripts/asynx.py" image batch-convert \
  --input-dir source --output-dir converted --format webp --recursive
```

支持图片信息、格式转换、按单边等比缩放或指定宽高缩放、裁剪、网格切图、联系表、Alpha/灰度 Mask 透明化和目录批量转换。自动识别主体的 AI 抠图、OCR、视频处理和语义编辑仍属于远程模型能力，不由这些本地命令实现。

## 输入图片

- 本地路径和 Data URL 原样交给脚本。脚本会完整解码、校正 EXIF，并只做一次必要的尺寸归一化和一次 WebP Q82 编码。
- 只有用户明确要求保留原始格式或字节时才传 `--keep-reference-original`；该选项不能绕过限制。
- 固定限制：PNG/JPEG/WebP；源文件每张不超过 25 MB；处理后每张不超过 5 MB、最长边 1600 px、总像素不超过 2,560,000；每个任务最多 5 张；本地图片合计不超过 20 MB；JSON 请求体不超过 32 MB。一次编码后仍超过 5 MB 会直接拒绝。
- HTTP(S) URL 不在本地下载或转换，交给 Asynx 服务端校验。CLI 返回 `warnings` 时，简短提醒用户弱网上传可能变慢，不要再次压缩。

## 首次配置

缺少配置时，不要索要或接收对话中的 API Key。请让用户在自己的终端运行：

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
```

`<skill-dir>` 必须替换为当前已安装 skill 的绝对路径，不要使用依赖仓库工作目录的相对路径。正常配置只询问 API Key，并使用
公共 Asynx 服务；只有用户明确说明是自托管部署时，才使用 `configure --base-url URL`。

配置保存在当前 OS 用户的固定位置：macOS/Linux 为 `~/.config/asynx/config.json`，Windows 为
`%APPDATA%\Asynx\config.json`；同一用户的 Codex、Claude Code 和多份 `asynx-image` 默认共用它。`ASYNX_API_KEY` 环境变量优先，
但普通 shell `export` 重启后通常失效。遇到缺少 Key 或重启后失效时，先执行以下安全诊断，不要读取或输出完整 Key：

```bash
python3 "<skill-dir>/scripts/asynx.py" config status
python3 "<skill-dir>/scripts/asynx.py" doctor
```

只有用户要求验证网络和 Key 时才执行 `doctor --verify`。随后根据诊断给出的配置路径和绝对 `configure` 命令指导用户；交互式
配置必须由用户在自己的终端执行，Agent 的非交互子进程不能代跑。

## 单任务与本地记录

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<提示词>"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<编辑指令>" --image "/absolute/path/input.png"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<编辑指令>" --from-task "<task-id>"
```

根据需要添加 `--model`、`--image-size`、`--aspect-ratio`、`--quality`、`--count`、`--output-format`、`--reference`、`--mask`、
`--output-dir`、`--keep-reference-original` 或 `--detach`。Skill 会保存必要的本地任务记录，成功下载的结果会自动建立索引。

`--detach` 只快速受理并返回 Task ID，不代表任务已完成。中断、会话切换或需要稍后处理时使用本地任务接口：

```bash
python3 "<skill-dir>/scripts/asynx.py" task list
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task poll [TASK_ID]
python3 "<skill-dir>/scripts/asynx.py" task recover
python3 "<skill-dir>/scripts/asynx.py" task cancel "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" asset list "<task-id>"
```

`task list` 查询本地任务；`task status` 查看本地快照；`task poll` 刷新一个或全部未完成任务并下载已完成 Asset；`task recover` 修复上次中断的提交状态，不要重新提交已有幂等键的任务；`task cancel` 请求上游取消，返回 `cancel_requested` 时必须告知用户上游可能继续执行并计费。

用户说“刚才那张”“上一张”时，只查本地索引，不扫描磁盘或调用远程历史：

```bash
python3 "<skill-dir>/scripts/asynx.py" recent --latest
python3 "<skill-dir>/scripts/asynx.py" recent --query "<描述或 Task ID>"
```

确定 Task ID 后使用 `edit --from-task <task-id>`。`asset list` 或 `recent` 返回的本地文件不存在时，先报告缺失，再让用户选择重新下载或提供新图片。

## 批量任务

批量任务与单任务共用本地任务记录，Asynx 端每张图仍是独立 Task。创建批次后立即返回批次 ID，不要因为异步任务尚未完成而阻塞用户对话：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create \
  --prompt "<提示词>" \
  --total 12
```

批次会立即推进少量首批 Task，后续由 `batch poll` 周期性推进。用户追加时：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch add "<batch-id>" --total 5
```

省略批次 ID 时使用最近的活动批次。没有新参数时继承原批次模型、尺寸、比例、质量、格式和输入图；用户明确修改的参数才覆盖原设置。

`batch poll` 会继续提交待处理项、查询已提交项并下载已完成 Asset；不要重新提交已经存在的 Task。需要查看完整明细时添加 `--items`：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch status "<batch-id>" --items
```

用户询问批次进度时运行 `batch poll` 或 `batch status`；多个活动批次时必须明确批次 ID。批次之外的单 Task 使用 `task` 命令，不调用 `batch poll`。

支持暂停、恢复和取消：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch pause "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch resume "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch cancel "<batch-id>"
```

取消只向上游发出请求；已开始执行的 Task 可能继续运行并计费。若批次返回 `cancel_requested`，后续仍需用 `batch poll` 观察终态。

批次单项失败不应使其他项失败。汇总完成数量、结果图片和失败原因；不要无条件重试 `failed`、`timeout` 或 `canceled` 项。

## 结果与安全

脚本的 JSON 和进度日志供 Agent 判断执行结果，不直接作为用户回复。面向用户时：

- 成功后优先展示图片或结果文件；Codex 桌面端使用绝对路径展示图片。默认不罗列任务 ID、模型、计费字段和耗时，用户询问或排查问题时再提供。
- 查询进度时用“排队中”“正在生成”“已完成”等自然语言；批量任务说明完成、处理中和失败的数量，不逐条复述轮询日志。
- 失败时说明发生了什么、对本次操作的影响和下一步。根据实际错误区分密钥、权限、网络、参数和服务异常，不把所有失败都归因于密钥，不照搬上游英文或 JSON；原因不明时明确说明，不猜测。
- 配置保存成功不代表密钥已验证；模型目录检查通过不代表已验证生图权限、余额或实际生成。用户可回到对话中继续请求，无需理解配置字段。
- 结果降级、部分失败、下载失败和取消可能继续计费等影响使用的信息必须说明。`degraded` 应说明为服务标记的降级结果；只有返回了具体原因才解释原因，不自行断言画质变差。
- 等待中断不代表远端任务已取消。需要继续时由 Agent 查询已有任务；已有任务或生成成功但下载失败时，不为了恢复结果重新提交生图。

保留诊断信息供后续查询；需要联系支持时，再提供相关任务 ID 或请求 ID，不展示密钥。

## 版本更新

命令 JSON 中包含 `legacy_installation_notice` 时，先交付本次任务结果，再简短告知旧入口冲突。脚本只在输出结果前检查三个固定旧路径，不联网、不扫描其他目录；不要在调用前自行增加检查。没有此字段时无需提及旧版清理。

旧版名称为 `asx`。用户要求安装或升级且发现旧入口时，Agent 应完成旧入口清理后继续安装，不必将文件操作交给用户：

- 只检查本次安装目标对应的旧路径：Codex 的 `~/.agents/skills/asx`、`~/.codex/skills/asx`，Claude Code 的 `~/.claude/skills/asx`；Windows 使用当前用户主目录下的对应路径。不要在普通生图前扫描这些目录。
- 检查旧路径中的 `SKILL.md` 与 `scripts/asynx.py`，确认属于 Asynx；无法确认归属时先说明情况，不按目录名直接删除。
- 等待正在运行的旧版 CLI 命令退出，将旧入口移到所有 Agent 的 `skills` 扫描目录之外备份，再重跑新版仓库安装器。保留本地改动；符号链接只移动链接本身，不移动其目标。不要只把旧目录重命名为 `asynx-image`，也不要在 `skills` 内留下备份。
- 保留 API Key 配置、任务数据库、缓存及生成图片。完成后报告新入口与备份位置，提示刷新 Skill 列表；安装失败时保留备份并说明恢复方式。

旧版 `asx` 的 `update apply` 不能完成更名，需运行新版仓库安装器。没有安装或升级指令时，只提醒旧入口冲突，不移动文件。

图片任务结果包含 `update_notice` 时，先交付图片，再简短告知可升级的版本。此提示可以在后续任务中重复出现；脚本最多每 6 小时联网检查一次，其余使用缓存。不要在生图前额外检查版本。

用户询问版本时执行 `update status`；要求检查新版时执行 `update check`，立即重新检查用 `update check --force`。
用户要求升级时执行 `update apply`，默认更新已安装副本；只更新当前副本用 `--target current`。用户已明确要求升级时直接执行，不必再次确认。
用户需要恢复上一版或中断的更新时执行 `update rollback`。更新命令不需要 API Key。

未收到升级指令时只提示，不自行替换文件。遇到本地修改或未登记的安装副本时按命令结果说明原因，不自动添加 `--force`。
用户要求关闭更新提示时，可设置 `ASYNX_UPDATE_CHECK=off`。旧版没有更新命令时，在仓库运行安装器升级一次即可。
