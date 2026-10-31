# 视频操作

使用 `MiniMax-H3` 生成视频，支持文字描述、首尾帧和参考素材。单任务默认等待并下载结果；后台提交时加 `--detach`，之后使用原任务 ID 查询或等待。

命令中的 `<skill-dir>` 替换为当前 Skill 的绝对路径。Windows 可用 `py` 替代 `python3`。

## 生成与参数

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --prompt "镜头缓慢环绕桌上的机械键盘，伴随轻柔的按键声"
python3 "<skill-dir>/scripts/asynx.py" video generate --prompt "<视频描述>" --duration 10 --resolution 1080p --aspect-ratio 9:16
```

未指定时使用 5 秒、720p、16:9；分辨率可选 `720p`、`1080p`、`2K`。用户指定的时长直接传给 Asynx 校验，不自动取整、截断或改成其他时长。模型固定为 `MiniMax-H3`，不要自动切换其他视频模型。

声音沿用模型原生能力，对白、音效或配乐要求写入 Prompt；没有独立声音开关。下载成功不表示已经检查音轨或声音内容。用户要求验收时，再对指定内容进行检查。

视频不使用图片命令的 `--quality`、`--image-size`、`--count` 或 `--output-format`。

## 首尾帧与参考素材

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --first-frame "/absolute/path/start.png" --prompt "保持主体外观，镜头缓慢靠近"
python3 "<skill-dir>/scripts/asynx.py" video generate --first-frame "/absolute/path/start.png" --last-frame "/absolute/path/end.png" --prompt "从首帧自然过渡至尾帧"
python3 "<skill-dir>/scripts/asynx.py" video generate --reference "/absolute/path/style.png" --reference "/absolute/path/subject.jpg" --prompt "参考第一张的风格，围绕第二张主体生成视频"
python3 "<skill-dir>/scripts/asynx.py" video generate --reference-video "https://example.com/reference.mp4" --reference-audio "https://example.com/reference.mp3" --prompt "<参考素材关系与视频要求>"
```

- `--first-frame` 和 `--last-frame` 明确首尾帧；尾帧必须同时提供首帧。
- 普通参考图使用 `--reference`，多张时重复参数，不把参考图默认当作首帧。
- 图片可用本地路径、Data URL 或 HTTP(S) URL。本地图交给脚本校正方向、缩放并一次编码为 JPEG/PNG，无需 Agent 预处理。
- 视频参考只接受 HTTP(S) MP4 URL，音频参考只接受 HTTP(S) MP3/WAV URL。当前不接收本地视频或音频路径，不自动上传到第三方文件服务。
- 脚本处理本地图片，其余素材限制由 Asynx 校验。参考素材超限时说明原因，不反复压缩、裁切或改变用户的素材内容。

这是参考素材生成新视频，不提供 `video edit`，也不提供本地剪辑、转码或拼接命令。

## 复用已有图片

```bash
python3 "<skill-dir>/scripts/asynx.py" video generate --from-task "<image-task-id>" --prompt "保持图片中的产品外观，生成缓慢环绕的展示视频"
```

优先使用当前对话已经确定的图片。`--from-task` 使用该任务单一的本地图片结果作为首帧；多个结果时先选定一张，再用 `--first-frame` 传文件路径，不自行把多张结果一起提交。查找素材可用 `recent --query "<描述或任务 ID>"`，核对类型和描述，不扫描磁盘。

## 查询、恢复与交付

```bash
python3 "<skill-dir>/scripts/asynx.py" task status "<task-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" wait "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task recover "<task-id>"
python3 "<skill-dir>/scripts/asynx.py" task cancel "<task-id>"
```

普通生成直接等待同一脚本进程，不另起命令重复查询。用户只查进度时使用 `task status --refresh`；提交中断且用户要求继续时才使用 `task recover`。下载失败可用 `wait` 获取原结果，不重新生成。

直接交付返回的 `files`，其中 `videos` 为视频，`images` 为图片附件，`media` 标明素材类型。支持本地视频展示的宿主可使用绝对路径展示视频，否则提供文件链接。不要把封面图片当作视频交付，也不要猜测未返回的时长、尺寸或音轨信息。

等待中断不等于取消。请求取消后，上游任务仍可能执行并计费；收到 `delayed` 也不要重新提交。部分失败和下载错误如实说明。

## 视频批次

```bash
python3 "<skill-dir>/scripts/asynx.py" batch create --operation video.generate --prompt "<视频描述>" --total 3 --duration 5 --resolution 720p
python3 "<skill-dir>/scripts/asynx.py" batch add "<batch-id>" --total 2 --prompt "<新的描述>" --duration 10
python3 "<skill-dir>/scripts/asynx.py" batch status "<batch-id>" --refresh
python3 "<skill-dir>/scripts/asynx.py" batch wait "<batch-id>"
```

`--total` 是视频任务数，创建时可传首尾帧和参考素材参数；批次不使用 `--from-task`，需要复用图片时传明确文件路径。追加默认继承原批次参数，可覆盖时长、分辨率、比例及输入素材，一次覆盖不改写原模板，不更换批次模型或类型。

暂停、继续、取消和追加的通用规则见[批次操作](operations.md#批次)。纯查询不会提交新视频任务；`batch wait` 会继续提交待处理项。没有执行进程时，已提交的远端任务继续执行，未提交项不会自行推进。

单任务不能直接追加，已完成的批次也不再接受追加；后续创作需新建批次并显式复用原图片和参数。两条不同镜头分别指定 Prompt；`--total 2` 表示按同一描述创建两个任务。
