# 操作参考

按当前需求阅读对应章节：[批次](#批次)、[任务恢复与取消](#任务恢复与取消)、[本地图片工具](#本地图片工具)、[输入图片](#输入图片)、[生成参数](#生成参数)。

命令中的 `<skill-dir>` 替换为当前已安装 Skill 的绝对路径，Windows 可用 `py` 替代 `python3`。文件路径使用绝对路径并完整引用。

## 批次

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create --prompt "<描述>" --total 12
python3 "<skill-dir>/scripts/asynx.py" batch add "<batch-id>" --total 5
python3 "<skill-dir>/scripts/asynx.py" batch list
python3 "<skill-dir>/scripts/asynx.py" batch status "<batch-id>" --items
python3 "<skill-dir>/scripts/asynx.py" batch poll "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch wait "<batch-id>" --max-wait 60
```

`--total` 是任务数，`--count` 是每个任务的输出图片数，默认每任务一张。创建和追加会立即推进少量任务，默认最多提交 4 项，结果中保留批次 ID 供后续操作。

批次默认先返回受理进度；用户要求等完时，继续 `batch wait` 或周期性 `batch poll`。`--max-wait` 只限制本次等待时间，超时不代表任务失败或取消。Skill 没有常驻调度器：没有后续调用时，已提交的远端任务继续执行，待提交项不会自动推进；只有宿主确实支持并已安排持续执行时，才承诺后台继续整个批次。

| 操作 | 实际作用 |
| --- | --- |
| `batch status` | 读取本地快照，不发起新任务 |
| `batch poll` / `batch wait` | 继续提交待处理项，刷新已提交项并下载结果 |
| `batch status --refresh` | 也会调用轮询并推进提交，不是纯查询 |
| `batch pause` | 暂停后续提交，不停止已在远端运行的任务 |
| `batch resume` | 解除暂停；随后仍需轮询推进 |
| `batch cancel` | 停止未提交项，并向已提交的任务请求取消 |

询问“目前记录到哪了”可读快照并说明它不是实时状态；用户要求继续推进时用轮询。需要仅查询远端进度时，从 `batch status --items` 取得已有远端任务 ID，再用 `status "<remote-task-id>"` 查询；这不会更新批次快照。当前 `batch poll` 没有仅刷新模式，暂停的批次也只返回本地快照；不要为了查进度擅自恢复提交。批次统计和管理仍使用 `batch` 命令。

追加规则：
- 优先使用当前对话确定的批次 ID，不依赖全局最近记录。省略 ID 时，多个活动批次会报歧义；没有活动批次时会回退到最近批次，但已完成或已取消的批次不能追加。
- 模型和操作类型固定沿用原批次，不支持通过 `batch add` 换模型；需要换模型时创建新批次。新批次不会自动继承旧设置，应从当前对话或公开命令结果取得原 Prompt、输入图和其他必要参数，无法确定时询问，不读取数据库拼模板。
- Prompt、尺寸、比例、质量、格式、每任务张数和输入图可以在追加时覆盖，省略则继承原批次模板。一次追加的覆盖值不会改写原批次模板。
- 批量编辑使用 `batch create --operation edit --image "/absolute/path/input.png" --prompt "<修改>" --total 5`。

```bash
python3 "<skill-dir>/scripts/asynx.py" batch pause "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch resume "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch cancel "<batch-id>"
```

返回 `cancel_requested` 时仍需轮询终态；已经开始的任务可能继续执行并计费。单项失败不应重建整个批次。结果默认保存到 `generated-images/<批次 ID>/`。

## 任务恢复与取消

先使用当前对话的任务 ID；不明确时通过 `task list` 或 `recent --query` 定位，不直接恢复所有任务。

```bash
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task poll "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" wait "<remote-task-id>"
python3 "<skill-dir>/scripts/asynx.py" task cancel "<task-id>"
```

- 有远端 ID：刷新或等待原任务。生成成功但下载失败、文件丢失时，`task poll` 或 `wait` 可尝试下载原结果，不重新生图；远端文件也不可用时再说明情况并让用户选择新图片或重新生成。
- 无远端 ID 且提交中断：`task recover` 会复用已保存的请求和幂等键尝试提交。它不能指定单个任务，会检查最近一批记录，默认上限 50；`--limit` 是数量上限，不是任务过滤器。仅在用户要求的恢复范围覆盖这些记录时使用；否则说明当前不支持精确恢复该单项，保留记录，不扩大恢复范围。
- `task poll` 不带 ID 也会处理多条记录，包括已成功任务；已知目标时始终带 ID。
- 批次的恢复使用 `batch poll`，不要用全局 `task recover` 代替批次调度。
- 等待中断、暂停或发出取消请求都不等于远端已停止。保留原任务记录，不为同一中断请求重新生成幂等键。

用户明确查远端历史且本地记录不足时可执行 `history --limit 20`；普通“刚才那张”不需要远端历史。

## 本地图片工具

以下操作不调用 Asynx，不需要 API Key，也不写入任务记录：

```bash
python3 "<skill-dir>/scripts/asynx.py" image info "/absolute/path/input.png"
python3 "<skill-dir>/scripts/asynx.py" image convert "/absolute/path/input.png" --output "/absolute/path/output.webp" --format webp
python3 "<skill-dir>/scripts/asynx.py" image resize "/absolute/path/input.png" --output "/absolute/path/small.png" --width 1200
python3 "<skill-dir>/scripts/asynx.py" image crop "/absolute/path/input.png" --output "/absolute/path/crop.png" --box 0,0,800,800
python3 "<skill-dir>/scripts/asynx.py" image slice "/absolute/path/input.png" --output-dir "/absolute/path/tiles" --rows 3 --columns 3
python3 "<skill-dir>/scripts/asynx.py" image contact-sheet "/absolute/path/a.png" "/absolute/path/b.png" --output "/absolute/path/sheet.jpg"
python3 "<skill-dir>/scripts/asynx.py" image apply-mask "/absolute/path/input.png" --mask "/absolute/path/mask.png" --output "/absolute/path/cutout.png"
python3 "<skill-dir>/scripts/asynx.py" image batch-convert --input-dir "/absolute/path/source" --output-dir "/absolute/path/converted" --format webp --recursive
```

缩放、裁剪、切图、格式转换及应用已有 Alpha/灰度 Mask 使用本地工具；换背景、改造主体等语义编辑使用远程 `edit`。本地工具不自动识别主体抠图，也不提供 OCR 或视频处理。

## 输入图片

普通参考图支持 PNG/JPEG/WebP。本地路径和 Data URL 由脚本解码、校正 EXIF，必要时缩放一次，再以 WebP Q82 编码一次；不需要 Agent 先压缩。

| 限制 | 当前客户端上限 |
| --- | --- |
| 普通参考图源数据 | 每张 25 MiB |
| 处理后普通参考图 | 每张 5 MiB，最长边 1600 px，总像素 2,560,000 |
| 普通参考图数量 | 每任务最多 5 张，模型上限可能更低 |
| 本地图片与 Mask 合计 | 处理后数据最多 20 MiB |
| JSON 请求体 | 32 MiB |

一次编码仍超限会报错，不反复降低质量；收到上传开销 `warnings` 时简短说明弱网下可能变慢。`--keep-reference-original` 跳过重编码，但原图必须满足处理后的限制。

HTTP(S) 图片 URL 原样交给服务端拉取、校验，不由客户端下载和压缩。Windows 路径完整引用，例如 `--reference "E:\images\source.png"`。

远程编辑的 `--mask` 与本地 `image apply-mask` 不同：仅在模型支持时使用；本地 Mask 必须是含全透明和全不透明区域的 PNG，不超过 10 MiB，并满足同样的边长和像素限制。有本地源图时，还要求 Mask 不大于处理后的源图且宽高比一致。Mask 不会随参考图自动缩放；URL Mask 由服务端校验。

## 生成参数

生成和编辑支持 `--model`、`--image-size`、`--aspect-ratio`、`--quality`、`--count`、`--output-format`、`--output-dir` 和 `--detach`。单任务的客户端 `--count` 范围为 1–4，还需满足模型限制；批量数量用 `--total`。

默认模型和质量由脚本定义，当前为 `gpt-image-2.5-sunburst` 与 `auto`。画幅可按用途选择：竖版卡牌或海报可用 `2:3` / `9:16`，横幅或封面可用 `16:9`，实际取值以模型支持为准。特殊能力不明时查询 `models`，不要对普通请求增加前置检查。

输出中的 `timings` 用于用户要求的耗时排查；不要把准备、提交、等待和下载等诊断字段当作普通结果逐项复述。
