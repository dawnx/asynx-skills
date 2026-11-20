# 3D 模型操作

通过文字描述或一张参考图生成 3D 模型，结果为 GLB 文件，有预览图时一并下载。已生成的模型或用户提供的本地模型文件可继续做绑定检查、骨骼绑定、套用动画与格式转换，见[绑定、动画与格式转换](#绑定动画与格式转换)。单任务默认等待完成并下载结果；需要后台提交时加 `--detach`，之后使用原任务 ID 查询或等待。

命令中的 `<skill-dir>` 替换为当前 Skill 的绝对路径。Windows 可使用 `py` 替代 `python3`。

## 生成与参数

```bash
python3 "<skill-dir>/scripts/asynx.py" model generate --prompt "一只圆润的陶瓷茶壶，哑光白釉"
python3 "<skill-dir>/scripts/asynx.py" model generate --image "/absolute/path/teapot.png"
python3 "<skill-dir>/scripts/asynx.py" model generate --prompt "<模型描述>" --negative-prompt "<不希望出现的内容>" --max-faces 50000 --seed 3
```

`--prompt`（文字生成）与 `--image`（图片生成）必须且只能选择一种。图片生成只接受一张参考图，不接受文字描述；需要同时约束外观与文字要求时，先生成或编辑出符合要求的图片，再以该图生成模型。

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

未指定的参数不发送，由服务采用默认值（默认生成贴图与 PBR 材质）。生成的输出格式固定为 GLB；需要其他格式时对生成结果执行 `model convert`，不提供本地格式转换或模型编辑命令。3D 生成不使用图片命令的 `--quality`、`--image-size`、`--count`、`--output-format` 与 `--reference`。

## 模型选择

3D 模型名称由服务方配置，脚本没有内置默认名称。生成、绑定检查与骨骼绑定、动画、格式转换通常是不同的模型条目，每个条目的 `task_types` 列出它提供的操作（`model.generate`、`model.rig_check`、`model.rig`、`model.retarget`、`model.convert`）。

每条命令按自己的操作挑选模型：账户只有一个提供该操作的 3D 模型时可省略 `--model`；有多个时命令会停止并列出名称（`model_required`），由用户选定后通过 `--model` 传入；没有时返回 `no_compatible_model`，说明当前账户未开通该操作。查看可用模型、各自提供的操作及价格：

```bash
python3 "<skill-dir>/scripts/asynx.py" models --modality model
```

## 参考图

- 本地参考图支持 PNG、JPEG、WebP 静态图，源文件不超过 25 MiB。在上传限制内原样上传，超出时由脚本校正方向、缩小并编码，Agent 无需预处理；HTTP(S) 链接原样交给 Asynx 读取。
- 主体完整、背景干净的单物体图片效果更稳定。图片超限或格式不符时命令会说明原因，由用户决定是否更换素材。

复用已有图片任务的结果：

```bash
python3 "<skill-dir>/scripts/asynx.py" model generate --from-task "<image-task-id>"
```

`--from-task` 将该任务唯一一张本地静态图片作为参考图，不能与 `--image` 或 `--prompt` 同用；存在多个结果，或结果为 GIF、SVG、视频、3D 模型时，先选定一张图片再用 `--image` 传入路径。查找素材可使用 `recent --query "<描述或任务 ID>"`，使用前核对类型与描述。

## 绑定、动画与格式转换

四个后续操作以一个**已成功完成的任务**的模型为来源，通过 `--from-task` 传入该任务的本地或服务端任务 ID；`rig-check`、`rig` 与 `convert` 也可改用 `--file` 传入一个本地模型文件，见[本地模型文件](#本地模型文件)。`--from-task` 与 `--file` 二选一，不接受链接。

```bash
python3 "<skill-dir>/scripts/asynx.py" model rig-check --from-task "<generate-task-id>"
python3 "<skill-dir>/scripts/asynx.py" model rig --from-task "<generate-task-id>" --rig-type biped --skeleton mixamo --format glb
python3 "<skill-dir>/scripts/asynx.py" model animate --from-task "<rig-task-id>" --animation preset:walk --animation preset:slash --format glb
python3 "<skill-dir>/scripts/asynx.py" model convert --from-task "<task-id>" --format fbx
python3 "<skill-dir>/scripts/asynx.py" model rig --file "/absolute/path/robot.glb" --rig-type biped
```

| 命令 | 可用来源 | 参数 | 结果 |
| --- | --- | --- | --- |
| `model rig-check` | 生成任务或本地模型文件 | 无 | 只返回结论 `rig_check`，不交付文件 |
| `model rig` | 生成任务或本地模型文件 | `--rig-type`、`--skeleton`、`--format` | 带骨骼的模型文件，有预览图时一并下载 |
| `model animate` | 骨骼绑定任务 | `--animation`（必填，可重复，最多 5 个）、`--format` | 包含所选动画的一个模型文件，无预览图 |
| `model convert` | 生成、骨骼绑定或动画任务，或本地模型文件 | `--format`（必填） | 目标格式的模型文件，有预览图时一并下载 |

- `--rig-type`：`biped`、`quadruped`、`hexapod`、`octopod`、`avian`、`serpentine`、`aquatic`、`others`。`--skeleton`（骨骼命名规范）：`tripo`、`mixamo`、`actorcore`、`unity`、`unreal`、`vrm`，仅在 `--rig-type` 为 `biped` 或省略时可用。两者均可省略，省略时不发送，由服务决定。
- `rig` 与 `animate` 的 `--format`：`glb`（默认）或 `fbx`。`convert` 的 `--format`：`gltf`、`fbx`、`usdz`、`obj`、`stl`、`3mf`，必须指定；转换结果不能再作为来源，需要多种格式时分别对同一来源转换。
- `--animation` 为动画标识，例如 `preset:idle`、`preset:walk`、`preset:run`、`preset:quadruped:walk`。一个任务可套用 1–5 个不重复的动画，每个用一次 `--animation` 指定，例如走路、受击、攻击三个动作：`--animation preset:walk --animation preset:hurt --animation preset:slash`。多个动画合在同一个任务里交付一个模型文件，**按动画个数计费**（三个动画即三倍单价）；超过 5 个时分成多个任务提交。可用标识取决于骨骼类型，脚本不校验标识是否存在：标识不被支持时提交会被受理，随后任务失败；失败的任务不计费，按失败原因更换标识后重新提交。
- 四条命令同样支持 `--model`、`--output-dir`、`--idempotency-key`、`--detach`、`--max-wait` 与 `--quote`。绑定检查通常免费或费用很低；骨骼绑定、动画与格式转换均为付费任务，每提交一次计费一次。费用不明确或用户想先了解时加 `--quote`。

来源规则由脚本与服务共同检查：

- 本机有来源任务的记录时，脚本在提交前核对：类型不符返回 `model_source_incompatible`，尚未成功返回 `model_source_not_ready`（来源仍在进行时按 `next_action_args` 先 `wait`），均不提交、不扣费。本机没有记录的服务端任务 ID 原样提交，由服务判断。
- 服务拒绝时同样不创建任务（`task_created: false`）：`model_source_not_found` 表示任务不存在或不属于当前账户；`model_source_not_ready` 表示来源未成功或其模型文件已不可用；`model_source_incompatible` 表示来源类型不符，或当前没有渠道能处理该来源。
- 动画必须由完成骨骼绑定的同一上游渠道执行，脚本无法指定渠道。该渠道不可用时服务以 `model_source_incompatible` 拒绝：稍后原样再试，持续出现时请用户联系服务支持。重新绑定或重新生成都会再次计费，由用户决定。

### 本地模型文件

`--file` 接受一个本地 3D 模型文件：GLB、FBX、OBJ 或 STL，按文件扩展名（`.glb`、`.fbx`、`.obj`、`.stl`，不区分大小写）判断格式，单个文件不超过 150 MB（150,000,000 字节）。不接受 GLTF：GLTF 通常引用外部的 `.bin` 与贴图文件，请先导出为单文件的 GLB。

- 脚本只在本机检查扩展名与大小，不解析模型内容：扩展名不符返回 `unsupported_model_file`，超限返回 `model_file_too_large`，空文件返回 `empty_model_file`，路径不存在返回 `model_file_not_found`，均在上传前停止，不上传、不提交、不扣费。文件内容是否可用由上游判断，内容无效时任务失败且不计费。
- 通过检查后，脚本在提交前把文件上传到 Asynx，再以上传得到的文件提交任务；同一文件在上传有效期内重复使用时不再重复上传。上传在提交时进行（加 `--quote` 报价时也会先上传），大文件需要相应的上传时间。
- 服务拒绝时同样不创建任务：`file_not_found` 表示上传的文件已过期或不可用，脚本会自动重新上传一次并以同一请求再提交；仍被拒绝时重新运行原命令。任务受理后，服务会为排队中的任务保留该文件；若执行时文件仍已不可用，任务以 `input_asset_unavailable` 失败且不计费，重新运行原命令即可。
- 计费与使用任务来源时相同：骨骼绑定与格式转换每提交一次计费一次。

**本地模型不能直接套用动画**：动画只能套用在 Asynx 骨骼绑定任务的结果上，`model animate --file` 会返回 `model_file_needs_rig`，不上传、不提交。为本地模型做动画时先绑定骨骼，再用绑定任务的 ID 套用动画：

```bash
# 1. 可选：检查本地模型能否绑定骨骼（返回 rig_check，不交付文件）
python3 "<skill-dir>/scripts/asynx.py" model rig-check --file "/absolute/path/robot.glb"
# 2. 绑定骨骼（付费）。记下返回的 local_id 或 task_id
python3 "<skill-dir>/scripts/asynx.py" model rig --file "/absolute/path/robot.glb" --rig-type biped
# 3. 对骨骼绑定任务套用动画（付费，按动画个数计费）
python3 "<skill-dir>/scripts/asynx.py" model animate --from-task "<rig-task-id>" --animation preset:walk
```

只需要换格式时直接转换本地文件，例如 `model convert --file "/absolute/path/robot.stl" --format obj`。

### 完整流程示例

用户：“生成一个卡通宇航员，绑定骨骼，做一个走路动画，最后给我 FBX。”

```bash
# 1. 生成（付费）。记下返回的 local_id 或 task_id
python3 "<skill-dir>/scripts/asynx.py" model generate --prompt "一个卡通宇航员，站立姿态，四肢分明"
# 2. 绑定检查：返回 rig_check，不交付文件
python3 "<skill-dir>/scripts/asynx.py" model rig-check --from-task "<generate-task-id>"
# 3. riggable 为 true 时绑定骨骼（付费），可采用返回的 recommended_rig_type
python3 "<skill-dir>/scripts/asynx.py" model rig --from-task "<generate-task-id>" --rig-type biped
# 4. 对骨骼绑定任务套用动画（付费，按动画个数计费）
python3 "<skill-dir>/scripts/asynx.py" model animate --from-task "<rig-task-id>" --animation preset:walk
# 5. 将动画结果转换为 FBX（付费）
python3 "<skill-dir>/scripts/asynx.py" model convert --from-task "<animate-task-id>" --format fbx
```

每一步都是独立任务，各有自己的任务 ID；下一步的 `--from-task` 使用上一步返回的 `local_id` 或 `task_id`，不要把上一步下载的结果文件再用 `--file` 传入：那样会重新上传，且动画只接受骨骼绑定任务本身作为来源。只需要 FBX 的绑定或动画结果时，也可在第 3、4 步直接加 `--format fbx`，省去转换。

绑定检查的结果在 `rig_check` 中：

```json
{"ok": true, "status": "succeeded", "task_type": "model.rig_check", "files": [],
 "rig_check": {"riggable": false, "recommended_rig_type": null}}
```

`riggable: false` 是一次成功的检查，命令正常退出，不是错误，也没有文件可下载。此时向用户说明该模型不适合自动绑定；是否仍要绑定、或重新生成一个可绑定的模型（付费），由用户决定。`recommended_rig_type` 可能为 `null`，此时省略 `--rig-type`。绑定检查不是必经步骤，用户明确要求直接绑定时可跳过。

## 查询、恢复与交付

```bash
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task recover "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task cancel "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" recent --modality model
```

以上命令适用于生成与全部后续操作。生成通常需要数分钟，服务端截止时间默认为 30 分钟；任务结束后脚本立即取回结果。普通任务在同一脚本进程内等待，无需另起命令重复查询。等待被中断后使用 `wait` 取回原任务结果；重新提交会再次计费。提交中断、不确定是否已受理时使用 `task recover` 向服务确认（只查询，不重新提交）。`wait`、`task status --refresh` 与 `task poll` 对绑定检查任务返回 `rig_check`，不下载文件；该任务没有文件，不会出现在 `recent` 中，之后用 `task status "<task-id>"` 查看结论。

生成时间通常超过宿主单条命令的时长上限：可加 `--max-wait <秒>`（略小于上限），到时返回 `wait_timeout: true` 与仍在运行的任务，任务不会被取消，再按返回的 `next_action_args` 执行 `wait`。用户想先了解费用时加 `--quote`，只返回服务报价，不提交、不扣费。

直接交付返回的 `files`（绝对路径）：`models` 为模型文件，其 `format` 为实际保存的格式（`glb`、`fbx`、`gltf`、`obj`、`stl`、`usdz`、`3mf` 或 `zip`），`images` 为预览图，`media` 标明每个文件的类型。预览图为可选附件，生成与骨骼绑定通常附带，动画与格式转换通常没有：缺失或下载失败时仅附带 `preview_unavailable` 提示，模型仍正常交付，需要时可再次 `wait` 补下载。`images` 中的预览图不是模型。

格式转换的结果以服务实际交付的文件为准，不一定与请求的格式同名：请求 `gltf` 时通常交付单文件的 GLB（`format` 为 `glb`），请求 `obj` 时通常交付包含 `.obj`、`.mtl` 与贴图的 ZIP 压缩包（`format` 为 `zip`）。脚本按服务交付的原样保存，不解压；交付该压缩包并说明其中包含所需格式的模型，用户要求时再解压。

模型文件按服务交付的原样保存，脚本不检查文件内部结构，只把下载到的大小与服务记录的文件大小比对；单个文件上限 512 MiB，与视频一致。大小不符（`incomplete_model_file`）时报告下载失败且不保存为模型，此时任务已完成，使用 `wait` 重新下载原任务即可。脚本不解析模型内容，面数、贴图、材质、骨骼与动画只说明结果中返回的内容；宿主无法预览模型时提供文件路径与预览图。

等待中断不等于取消。发出取消请求后，上游任务仍可能继续执行并计费。`delayed` 表示任务仍在进行，继续等待原任务。失败与下载错误须如实说明。

## 批次

3D 模型不支持 `batch`。需要多个模型、超过 5 个动画或多种格式时，在用户确认的数量与费用范围内逐个提交对应命令（可加 `--detach`），分别用各自的任务 ID 查询与下载。
