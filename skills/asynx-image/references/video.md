# 视频操作

生成视频，支持文字描述、首尾帧与参考素材。单任务默认等待完成并下载结果；需要后台提交时加 `--detach`，之后使用原任务 ID 查询或等待。

命令中的 `<skill-dir>` 替换为当前 Skill 的绝对路径。Windows 可使用 `py` 替代 `python3`。

## 生成与参数

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --prompt "镜头缓慢环绕桌上的机械键盘，伴随轻柔的按键声"
python3 "<skill-dir>/scripts/asynx.py" video generate --prompt "<视频描述>" --duration 10 --resolution 1080p --aspect-ratio 9:16
```

未指定时默认为 5 秒、720p、16:9；分辨率可选 `720p`、`1080p`、`2K`。用户指定的时长原样发送，由 Asynx 校验。未指定模型时省略 `--model`，由服务选用默认视频模型；用户指定时传 `--model "<模型名>"`，可用模型见 `models --modality video`。

声音沿用模型原生能力，对白、音效或配乐要求写入提示词，无独立声音开关。下载成功不代表已检查音轨或声音内容。用户要求验收时，再针对其指定的内容检查。

视频不使用图片命令的 `--quality`、`--image-size`、`--count` 与 `--output-format`。

## 首尾帧与参考素材

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --first-frame "/absolute/path/start.png" --prompt "保持主体外观，镜头缓慢靠近"
python3 "<skill-dir>/scripts/asynx.py" video generate --first-frame "/absolute/path/start.png" --last-frame "/absolute/path/end.png" --prompt "从首帧自然过渡至尾帧"
python3 "<skill-dir>/scripts/asynx.py" video generate --reference "/absolute/path/style.png" --reference "/absolute/path/subject.jpg" --prompt "参考第一张的风格，围绕第二张主体生成视频"
python3 "<skill-dir>/scripts/asynx.py" video generate --reference-video "https://example.com/reference.mp4" --reference-audio "https://example.com/reference.mp3" --prompt "<参考素材关系与视频要求>"
```

- `--first-frame` 与 `--last-frame` 分别指定首帧与尾帧；使用尾帧时须同时提供首帧。
- 普通参考图使用 `--reference`，多张时重复该参数；只有 `--first-frame` 传入的图片才作为首帧。
- 图片可使用本地路径、Data URL 或 HTTP(S) 链接。本地图交由脚本校正方向、缩放并一次性编码为 JPEG/PNG，再单独上传后由任务引用，Agent 无需预处理。
- 视频参考仅接受 HTTP(S) 的 MP4 链接，音频参考仅接受 HTTP(S) 的 MP3/WAV 链接。当前不支持本地视频或音频路径，也不会自动上传至第三方文件服务。
- 本地图片由脚本处理，其余素材限制由 Asynx 校验。素材超限时命令会说明原因，由用户决定是否更换素材。

本功能为基于参考素材生成新视频，不提供 `video edit`，也不提供本地剪辑、转码或拼接命令。

## 复用已有图片

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --from-task "<image-task-id>" --prompt "保持图片中的产品外观，生成缓慢环绕的展示视频"
```

优先使用当前对话已确定的图片。`--from-task` 将该任务唯一一张本地图片作为首帧；存在多个结果时命令会停止（`ambiguous_video_source`），选定一张后用 `--first-frame` 传入路径。查找素材使用 `recent --query "<描述或任务 ID>"`，使用前核对类型与描述。

## 查询、恢复与交付

```bash
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task recover "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task cancel "<task-id>"
```

普通生成在同一脚本进程内等待，无需另起命令重复查询。用户仅查询进度时使用 `task status --refresh`；提交中断、不确定是否已受理时使用 `task recover` 向服务确认（只查询，不重新提交）。下载失败使用 `wait` 取回原结果，不产生新任务。

直接交付返回的 `files`，其中 `videos` 为视频，`images` 为图片附件，`media` 标明素材类型。宿主支持本地播放视频时可用绝对路径展示，否则提供文件链接。`images` 中的封面图不是视频；时长、尺寸与音轨只说明结果中返回的内容。

等待中断不等于取消。发出取消请求后，上游任务仍可能继续执行并计费。`delayed` 表示任务仍在进行，继续等待原任务。部分失败与下载错误须如实说明。

## 视频批次

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create --operation video.generate --prompt "<视频描述>" --total 3 --duration 5 --resolution 720p
python3 "<skill-dir>/scripts/asynx.py" batch add "<batch-id>" --total 2 --prompt "<新的描述>" --duration 10
python3 "<skill-dir>/scripts/asynx.py" batch status "<batch-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" batch wait "<batch-id>"
```

`--total` 为视频任务数，创建时可传入首尾帧与参考素材参数；批次不支持 `--from-task`，需复用图片时传入明确的文件路径。追加默认继承原批次参数，可覆盖时长、分辨率、画幅比例与输入素材；单次覆盖不会改写原模板，也不能更换批次的模型或类型。

暂停、继续、取消与追加的通用规则见[批次操作](operations.md#批次)。纯查询不会提交新视频任务；`batch wait` 会继续提交待处理项。无执行进程时，已提交的远端任务继续执行，未提交项不会自动推进。

单任务不可直接追加，已完成的批次也不再接受追加；后续创作需新建批次并显式复用原图片与参数。两个不同镜头须分别指定提示词；`--total 2` 表示按同一描述创建两个任务。
