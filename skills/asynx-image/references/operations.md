# 操作参考

按需阅读。代码块是完整命令；命令表格省略前缀 `python3 "<skill-dir>/scripts/asynx.py"`。文件路径使用绝对路径并加引号。

## 批次

`--total` 表示独立任务数，`--count` 表示每个任务的输出数量或组图上限；二者不能互换。对只支持单图的模型，明确授权生成多张独立结果后使用 `--count 1 --total N`，不可承诺跨任务内容严格一致，也不能因组图请求失败擅自增加付费提交。

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create --prompt "<描述>" --total 12
python3 "<skill-dir>/scripts/asynx.py" batch add "<batch-id>" --total 5 --prompt "<新增描述>"
```

创建和追加会开始提交任务。`--total` 每次可创建或追加 1–1000 项，`--count` 是每个图片任务的输出张数。`create`、`add`、`poll` 单次默认最多提交 4 项，可用 `--limit` 调整。批量编辑使用 `batch create --operation edit --image "<图片路径>"`。

每项描述或参数不同时，用清单一次建好，不要逐项 `batch add`：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create --items-file "/absolute/path/items.jsonl" --aspect-ratio 16:9
```

清单为 JSON Lines（每行一项）或 JSON 数组，最多 1000 项。每项是一条提示词字符串，或含 `prompt` 的对象；对象可覆盖 `aspect_ratio`、`image_size`、`size`、`quality`、`count`（视频批次为 `duration`、`resolution`、`aspect_ratio`），其余参数与参考图沿用命令行。`--items-file` 与 `--prompt`、`--total` 不同时使用。

```text
"红色机械键盘，白底产品图"
{"prompt": "同款键盘，深绿色外壳", "image_size": "2K"}
{"prompt": "同款键盘，方形主图", "size": "1024x1024"}
```

`batch create ... --quote` 只返回整批的服务报价（按计价参数分组列出），不创建批次、不提交、不扣费。带本地参考图报价时，图片会先上传，上传不产生费用。批次结果中的 `billing` 汇总已完成任务的实际扣费。

| 命令 | 作用 |
| --- | --- |
| `batch list` | 列出本机批次 |
| `batch status <id> --refresh` | 查询已提交任务并下载结果，不提交新任务；`--items` 显示明细 |
| `batch wait <id>` | 持续提交剩余项目并等待完成；`--max-wait` 为最多等待秒数，0 表示不限时 |
| `batch poll <id> --limit 4` | 推进一次，最多提交指定数量 |
| `batch pause <id>` | 暂停后续提交，已提交任务继续运行 |
| `batch resume <id>` | 解除暂停，再用 `batch wait` 继续 |
| `batch cancel <id>` | 停止未提交项目并请求取消已提交任务 |

批次固定模型与操作类型，只能向进行中或暂停的批次追加。省略参数沿用原模板，显式参数仅影响新增项目；换模型需新建批次。未指定模型的批次使用服务的默认模型，而默认模型随输出格式不同，因此不能在普通图片与 SVG/GIF 之间原地切换，须新建批次。允许的格式切换会清理继承的格式专属参数；更换编辑原图须重新提供对应的 `--markup` 或 `--mask`，不再需要图层时新建批次。

使用当前对话确认的批次 ID。返回 `batch.files` 可直接交付，失败与下载问题见返回详情；单项失败不重做整批。进程停止后，未提交项目不会自动推进。

提交受阻时批次不会无限重试：

批次按服务返回的 `next_step` 处理提交错误：

- `retry_later`：账户并发已满时，剩余项目保持待提交，下一轮继续，批次不暂停；模型暂不可用或网络持续失败时逐轮延长间隔，连续 5 轮仍失败后自动暂停。
- `fix_account`、`contact_support`：立即自动暂停批次，剩余项目不再提交。
- `change_request`：只把被拒绝的那一项记为失败，其余项目继续。
- 自动暂停时返回 `auto_paused: true`，原因见 `batch.submission_blocked` 与 `action_required`。向用户说明原因，问题解决后执行 `batch resume` 再 `batch wait`。
- 某一项此前的提交没有得到答复时，批次会先向服务查询该项是否已创建任务，再决定是否发送，因此同一项不会变成两个任务。

## 任务恢复与取消

| 情况 | 操作 |
| --- | --- |
| 生成完成但下载失败、文件丢失 | `wait <id>` 获取原结果 |
| 提交中断、尚未确认是否受理 | `task recover <id>` 向服务查询原提交，再按返回结果继续 |
| 错误带 `task_created: false` | 服务确认未创建、未扣费，无需恢复；按 `next_step` 处理后重新提交 |
| 用户要求取消 | `task cancel <id>`；批次使用 `batch cancel <id>` |
| 本地记录不足，需查远端历史 | `history --limit 20` |

`task recover` 按原请求标识向服务查询，不发送任何生成请求，也不会产生费用；不带 ID 时检查所有尚未确认的记录。结果有三种：

- 查到原任务：绑定到本地记录，继续等待并下载结果。
- 确认没有创建：记录标为失败并返回 `task_created: false`。用户仍需要结果时再重新提交。
- `submission_pending`：原请求发出不足 3 分钟，可能仍在途中；稍后再执行一次，不要重新生成。

带 `--idempotency-key` 重复提交、以及批次重新发送某一项之前，脚本同样会先做这一查询。

取消请求不保证远端立即停止，已开始的任务可能继续计费。

## 本地图片工具

仅处理静态 PNG、JPEG、WebP，无需 API Key。

| 命令 | 用途 |
| --- | --- |
| `image info "<图片>"` | 查看尺寸和格式 |
| `image convert "<图片>" --output "<输出.webp>" --format webp` | 格式转换 |
| `image resize "<图片>" --output "<输出>" --width 1200` | 按比例缩放；也可指定高度 |
| `image crop "<图片>" --output "<输出>" --box 0,0,800,800` | 按左、上、右、下坐标裁剪 |
| `image slice "<图片>" --output-dir "<目录>" --rows 3 --columns 3` | 等分切图 |
| `image contact-sheet "<图1>" "<图2>" --output "<输出>"` | 预览拼图 |
| `image apply-mask "<图片>" --mask "<蒙版>" --output "<输出.png>"` | 应用已有 Alpha/灰度蒙版 |
| `image batch-convert --input-dir "<目录>" --output-dir "<目录>" --format webp` | 批量转换；`--recursive` 包含子目录 |

本地工具不提供自动抠图、补边、SVG/GIF 编辑或视频处理。以静态参考图生成 GIF 可使用远程 `edit --image ... --output-format gif`，不等于直接修改已有 GIF 的帧或时序。尺寸被拒绝时，不自动改为本地加工；用户同意调整静态图后，按需求缩放或裁剪，并区分生成尺寸与交付尺寸。

## 输入图片

本地文件与 Data URL 输入限静态 PNG、JPEG、WebP，脚本会校正方向、缩放并压缩，处理后的图片先单独上传到服务，任务请求只引用上传后的文件；HTTP(S) URL 由服务端校验，透传不代表格式受支持。`--keep-reference-original` 跳过压缩，但须满足下表的处理后参考图限制。

| 项目 | 客户端上限 |
| --- | --- |
| 原始参考图 | 每张 25 MiB |
| 处理后参考图 | 每张 5 MiB，最长边 1600 px，总像素 2,560,000 |
| 参考图数量 | 每任务 16 张 |
| 处理后的本地参考图与蒙版或标注合计 | 20 MiB；蒙版与标注互斥 |
| 标注 PNG | 原始及处理后均不超过 10 MiB |

服务端可能设置更低上限。URL 编辑或多张图片比例不一致时，须明确输出比例或像素尺寸。

同一张图片对同一账户只上传一次：批次中共用参考图的各项、同一图片的再次使用都复用已上传的文件，文件临近过期或服务端已不存在时脚本会自动重新上传，无需用户处理。处理后的图片在提交前暂存于本机缓存目录（见[本机数据与保存目录](setup.md#本机数据与保存目录)）。提示 `reference_image_cache_missing` 表示暂存副本已被删除，须用原图片文件重新创建任务或批次。

编辑蒙版用 `--mask`：本地文件与 Data URL 须为 PNG、含全透明与全不透明区域、最多 10 MiB，并满足同样边长和像素限制。原图尺寸可读取时，蒙版不得大于处理后的原图且比例须一致；脚本不自动缩放蒙版，URL 蒙版由服务端校验。

## 生成参数

模型、尺寸及默认值遵循 [SKILL.md](../SKILL.md#生成与编辑)。单任务 `--count` 为 1–10 张，更多数量使用批次。静态图仅在用户要求自动尺寸时传 `--size auto`。

未指定 `--output-dir` 时，结果保存到默认目录下以“日期-描述-短 ID”命名的子目录；默认目录为当前目录的 `generated-images`，可在[配置](setup.md#本机数据与保存目录)中调整。用户指定了保存位置时传 `--output-dir`。

### 高级图片参数与结果格式

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "咖啡杯矢量图标" --output-format svg
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "小猫挥手，无缝循环" --output-format gif --size auto --frame-count 8 --frame-duration-ms 100
```

| 参数 | 用途 |
| --- | --- |
| `--background transparent` | 请求透明背景，位图使用 PNG/WebP；SVG 也可透明，JPEG 不支持 |
| `--quality` | 显式值原样发送。省略时脚本不补值，由服务按所用模型采用默认值 |
| `--complexity`、`--moderation`、`--safety-identifier` | 按需传入所选服务支持的值 |
| `--output-compression` | 压缩参数 0–100，适用于 JPEG/WebP |
| `--frame-count`、`--frame-duration-ms` | 仅用于 GIF。客户端只校验通用边界（2–64 帧、20–2000 ms），模型实际范围见 [GIF 动画](#gif-动画) |
| `--svg-model`、`--svg-reasoning-effort` | 仅在用户要求时指定 SVG 编写模型或推理强度；省略编写模型时由服务选用默认值 |
| `--markup "<标注.png>"` | 仅用于编辑；原图与标注支持本地文件或 Data URL，不支持 HTTP(S) URL。标注须为等大透明通道 PNG，与原图同步缩放，不能同时使用 `--mask` |
| `--params-file "<参数.json>"` | 提供嵌套高级参数；命令参数覆盖同名字段，`size` 与档位/比例的互斥规则仍适用 |

参数文件仅接受 `size`、`complexity`、`background`、`output_compression`、`moderation`、`safety_identifier`、`markup`、`frame_count`、`frame_duration_ms`、`svg`、`optimize_prompt_options`；不接受认证、地址或任务身份字段。上述参数也适用于图片批次，视频操作见[视频参考](video.md)。

### 提示词优化模式

已有模型说明确认支持时直接复用；首次使用且缺少能力信息时才查询 `models describe`。需示例再加 `--examples`。可用 `--optimize-prompt-mode standard` 或 `fast`。参数文件对应 `{"optimize_prompt_options":{"mode":"fast"}}`；仅接受 `mode` 这一字段及这两个值，显式 CLI 优先。脚本会将其映射到 `input.extra.optimize_prompt_options`，参数文件不开放任意 `extra`，也不能覆盖服务、认证、模型或数量。批次追加继承原模式；显式新模式只影响追加任务。省略不强制选择模式，上游拒绝时不自动换值或重提。

### GIF 动画

`generate`，或以静态图为输入的 `edit`，都可请求 GIF；GIF/SVG 文件不能作为参考图输入。帧参数仅用于 GIF，其他格式携带时脚本直接拒绝。这不等于修改已有 GIF 的帧或时序。

尺寸：未指定任何尺寸时，单任务和新建批次均保存 `size=auto`，编辑也不继承原图比例；显式 `--size` 与 `--image-size`/`--aspect-ratio` 互斥，明确参数不会被自动尺寸覆盖。批次追加先继承原模板尺寸再应用覆盖；查询、等待、恢复已受理任务不重算其保存参数。

客户端只校验通用类型与资源边界，不按模型名猜测协议；所选模型是否支持 GIF 及参数组合由服务判断，客户端接受某值不代表模型支持。被拒绝后不自动猜尺寸重提。

OpenAI GPT Image 的已知限制（2026-11-07 核对[官方 GIF 指南](https://developers.openai.com/api/docs/guides/image-generation#generate-animated-gif-output)，以官方文档和模型说明为准）：

- 2–16 帧，默认 9 帧、200 ms；帧时长 20–2000 ms，按 10 ms 取整。
- `size` 为 `auto` 或宽高均为 16 倍数的像素尺寸。帧数、每帧尺寸与输出数量共同受请求总像素预算限制，文档未公开完整数值；不要把静态图尺寸上限当作 GIF 承诺，也不保证 `auto` 返回某个固定尺寸或比例。
- 不支持 Mask、自定义 `output_compression`、流式预览与 partial images。
- 质量与 complexity 分别控制渲染与规划，均不提供骨骼或逐帧姿态约束；增加帧数不保证动作正确或循环无缝。
