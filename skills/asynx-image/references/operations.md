# 操作参考

按需阅读。代码块是完整命令；命令表格省略前缀 `python3 "<skill-dir>/scripts/asynx.py"`。文件路径使用绝对路径并加引号。

## 批次

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create --prompt "<描述>" --total 12
python3 "<skill-dir>/scripts/asynx.py" batch add "<batch-id>" --total 5 --prompt "<新增描述>"
```

创建和追加会开始提交任务。`--total` 每次可创建或追加 1–1000 项，`--count` 是每个图片任务的输出张数。`create`、`add`、`poll` 单次默认最多提交 4 项，可用 `--limit` 调整。批量编辑使用 `batch create --operation edit --image "<图片路径>"`。

| 命令 | 作用 |
| --- | --- |
| `batch list` | 列出本机批次 |
| `batch status <id> --refresh` | 查询已提交任务并下载结果，不提交新任务；`--items` 显示明细 |
| `batch wait <id>` | 持续提交剩余项目并等待完成；`--max-wait` 为最多等待秒数，0 表示不限时 |
| `batch poll <id> --limit 4` | 推进一次，最多提交指定数量 |
| `batch pause <id>` | 暂停后续提交，已提交任务继续运行 |
| `batch resume <id>` | 解除暂停，再用 `batch wait` 继续 |
| `batch cancel <id>` | 停止未提交项目并请求取消已提交任务 |

批次固定模型与操作类型，只能向进行中或暂停的批次追加。省略参数沿用原模板，显式参数仅影响新增项目；换模型需新建批次。`gpt-image-2.5-sunburst` 批次不能原地切换为 SVG/GIF，须新建批次。允许的格式切换会清理继承的格式专属参数；更换编辑原图须重新提供对应的 `--markup` 或 `--mask`，不再需要图层时新建批次。

使用当前对话确认的批次 ID。返回 `batch.files` 可直接交付，失败与下载问题见返回详情；单项失败不重做整批。进程停止后，未提交项目不会自动推进。

## 任务恢复与取消

| 情况 | 操作 |
| --- | --- |
| 生成完成但下载失败、文件丢失 | `wait <id>` 获取原结果 |
| 提交中断、尚未确认是否受理 | `task recover <id>` 确认原提交，再按返回指引继续 |
| 用户要求取消 | `task cancel <id>`；批次使用 `batch cancel <id>` |
| 本地记录不足，需查远端历史 | `history --limit 20` |

`task recover` 不带 ID 会处理多条记录，仅用于用户明确要求批量恢复；批次继续使用批次命令。取消请求不保证远端立即停止，已开始的任务可能继续计费。

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

不提供自动抠图、补边、SVG/GIF 编辑或视频处理。尺寸被拒绝时，不自动改为本地加工；用户同意调整静态图后，按需求缩放或裁剪，并区分生成尺寸与交付尺寸。

## 输入图片

本地文件与 Data URL 输入限静态 PNG、JPEG、WebP，脚本会校正方向、缩放并压缩；HTTP(S) URL 由服务端校验，透传不代表格式受支持。`--keep-reference-original` 跳过压缩，但须满足下表的处理后参考图限制。

| 项目 | 客户端上限 |
| --- | --- |
| 原始参考图 | 每张 25 MiB |
| 处理后参考图 | 每张 5 MiB，最长边 1600 px，总像素 2,560,000 |
| 参考图数量 | 每任务 16 张 |
| 处理后的本地参考图与蒙版或标注合计 | 20 MiB；蒙版与标注互斥 |
| 标注 PNG | 原始及处理后均不超过 10 MiB |
| 整个请求 | 32 MiB |

服务端可能设置更低上限。URL 编辑或多张图片比例不一致时，须明确输出比例或像素尺寸。

编辑蒙版用 `--mask`：本地文件与 Data URL 须为 PNG、含全透明与全不透明区域、最多 10 MiB，并满足同样边长和像素限制。原图尺寸可读取时，蒙版不得大于处理后的原图且比例须一致；脚本不自动缩放蒙版，URL 蒙版由服务端校验。

## 生成参数

模型、尺寸及默认值遵循 [SKILL.md](../SKILL.md#生成与编辑)。单任务 `--count` 为 1–10 张，更多数量使用批次；`--output-dir` 指定保存目录。仅在用户要求自动尺寸时传 `--size auto`，不将其当作省略参数。

### 高级图片参数与结果格式

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "咖啡杯矢量图标" --output-format svg
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "小猫挥手" --output-format gif --frame-count 8 --frame-duration-ms 100
```

| 参数 | 用途 |
| --- | --- |
| `--background transparent` | 请求透明背景，位图使用 PNG/WebP；SVG 也可透明，JPEG 不支持 |
| `--complexity`、`--moderation`、`--safety-identifier` | 按需传入所选服务支持的值 |
| `--output-compression` | 压缩参数 0–100，适用于 JPEG/WebP |
| `--frame-count`、`--frame-duration-ms` | GIF 帧数 2–64，每帧 20–2000 毫秒 |
| `--svg-model`、`--svg-reasoning-effort` | 仅在用户要求时覆盖 SVG 编写模型或推理强度 |
| `--markup "<标注.png>"` | 仅用于编辑；原图与标注支持本地文件或 Data URL，不支持 HTTP(S) URL。标注须为等大透明通道 PNG，与原图同步缩放，不能同时使用 `--mask` |
| `--params-file "<参数.json>"` | 提供嵌套高级参数；命令参数覆盖同名字段，`size` 与档位/比例的互斥规则仍适用 |

参数文件仅接受 `size`、`complexity`、`background`、`output_compression`、`moderation`、`safety_identifier`、`markup`、`frame_count`、`frame_duration_ms`、`svg`；不接受认证、地址或任务身份字段。上述参数也适用于图片批次，视频操作见[视频参考](video.md)。
