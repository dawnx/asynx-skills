# 3D 模型操作

通过文字描述或一张参考图生成 3D 模型，结果为 GLB 文件，有预览图时一并下载。单任务默认等待完成并下载结果；需要后台提交时加 `--detach`，之后使用原任务 ID 查询或等待。

命令中的 `<skill-dir>` 替换为当前 Skill 的绝对路径。Windows 可使用 `py` 替代 `python3`。

## 生成与参数

```bash
python3 "<skill-dir>/scripts/asynx.py" model generate --prompt "一只圆润的陶瓷茶壶，哑光白釉"
python3 "<skill-dir>/scripts/asynx.py" model generate --image "/absolute/path/teapot.png"
python3 "<skill-dir>/scripts/asynx.py" model generate --prompt "<模型描述>" --negative-prompt "<不希望出现的内容>" --max-faces 50000 --seed 3
```

`--prompt`（文字生成）与 `--image`（图片生成）必须且只能选择一种。图片生成只接受一张参考图，不接受文字描述；需要同时约束外观与文字要求时，先生成或编辑出符合要求的图片，再以该图生成模型。不得将用户的文字要求静默丢弃后改用图片生成。

| 参数 | 说明 |
| --- | --- |
| `--prompt` | 文字生成的模型描述，1–1024 个字符 |
| `--negative-prompt` | 不希望出现的内容，1–255 个字符，仅文字生成可用 |
| `--image` | 图片生成的参考图，本地路径或 HTTP(S) 链接，仅一张 |
| `--no-texture` | 不生成贴图，仅输出几何体 |
| `--no-pbr` | 生成贴图但不生成 PBR 材质 |
| `--max-faces` | 面数上限，1–2,000,000；是预算而非精确目标，实际面数可能更少 |
| `--seed` / `--texture-seed` | 几何与贴图的随机种子，0–2147483647；`--texture-seed` 不能与 `--no-texture` 同用 |
| `--model` | 3D 模型名称 |

未指定的参数不发送，由服务采用默认值（默认生成贴图与 PBR 材质）。输出格式固定为 GLB，不提供其他格式、本地格式转换或模型编辑命令。3D 生成不使用图片命令的 `--quality`、`--image-size`、`--count`、`--output-format` 与 `--reference`。

## 模型选择

3D 模型名称由服务方配置，脚本没有内置默认名称。账户只有一个可用 3D 模型时可省略 `--model`；有多个时命令会停止并列出名称，须由用户选定后通过 `--model` 传入，不得代为挑选。查看可用模型及价格：

```bash
python3 "<skill-dir>/scripts/asynx.py" models --modality model
```

## 参考图

- 本地参考图支持 PNG、JPEG、WebP 静态图，源文件不超过 25 MiB。脚本校正方向、按需缩小并编码后上传，Agent 无需预处理；HTTP(S) 链接原样交给 Asynx 读取。
- 主体完整、背景干净的单物体图片效果更稳定。图片超限或格式不符时说明原因，不得反复压缩、裁切或改动用户素材。

复用已有图片任务的结果：

```bash
python3 "<skill-dir>/scripts/asynx.py" model generate --from-task "<image-task-id>"
```

`--from-task` 将该任务唯一一张本地静态图片作为参考图，不能与 `--image` 或 `--prompt` 同用；存在多个结果，或结果为 GIF、SVG、视频、3D 模型时，先选定一张图片再用 `--image` 传入路径。查找素材可使用 `recent --query "<描述或任务 ID>"`，使用前核对类型与描述。

## 查询、恢复与交付

```bash
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task recover "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task cancel "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" recent --modality model
```

生成通常需要数分钟，服务端截止时间默认为 30 分钟；脚本约每 10 秒查询一次。普通生成在同一脚本进程内等待，无需另起命令重复查询。等待被中断后使用 `wait` 取回原任务结果，不得重新生成；仅在提交中断且用户要求继续时使用 `task recover`。

生成时间通常超过宿主单条命令的时长上限：可加 `--max-wait <秒>`（略小于上限），到时返回 `wait_timeout: true` 与仍在运行的任务，任务不会被取消，再按返回的 `next_action_args` 执行 `wait`。用户想先了解费用时加 `--quote`，只返回服务报价，不提交、不扣费。

直接交付返回的 `files`（绝对路径）：`models` 为 GLB 模型文件，`images` 为预览图，`media` 标明每个文件的类型。预览图为可选附件：缺失或下载失败时仅附带 `preview_unavailable` 提示，模型仍正常交付，需要时可再次 `wait` 补下载。不得将预览图当作模型交付。

脚本下载后核对 GLB 文件头与声明长度，单个文件上限 512 MiB，与视频一致。文件不完整（`incomplete_model_file`）或不是 GLB（`invalid_model_asset`）时报告下载失败且不保存为模型，此时生成已完成，使用 `wait` 重新下载原任务即可。脚本不解析模型内容，不得凭猜测补充结果中未返回的面数、贴图或材质信息；宿主无法预览 GLB 时提供文件路径与预览图。

等待中断不等于取消。发出取消请求后，上游任务仍可能继续执行并计费；收到 `delayed` 也不得重新提交。失败与下载错误须如实说明。

## 批次

3D 模型不支持 `batch`。需要多个模型时，在用户确认的数量与费用范围内逐个提交 `model generate`（可加 `--detach`），分别用各自的任务 ID 查询与下载。
