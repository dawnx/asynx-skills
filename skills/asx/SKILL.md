---
name: asx
description: 通过已配置的 Asynx API 生成、编辑和批量处理图片。用于生图、画图、参考图生成、图片编辑、局部重绘、批量生图、追加任务、查询进度、恢复中断任务，以及 GPT Image、Gemini Image、Seedream 请求。不要用于视频或非图片任务。
---

# Asynx 图片

使用本 skill 目录中的 `scripts/asynx.py` 完成所有 Asynx API 操作，不要自己拼接 curl。脚本只需要 Python 3.10 或更高版本，
API Key 不得出现在命令参数、Prompt、日志或项目文件中。

## 选择操作

- 用户要生成新图时使用 `generate`。
- 用户要修改、替换、删除或局部重绘已有图片时使用 `edit`，原图通过 `--image` 传入。
- 用户要批量生成或编辑时使用 `batch create`；用户说“再追加”“继续生成”“加入这个批次”时使用 `batch add`。
- 用户指定模型时保留其意图；未指定时让脚本使用默认模型。脚本会读取实时模型目录并拒绝不可用或含糊的模型选择。

本地输入图必须使用绝对路径，也支持 HTTP(S) URL。Mask 只能在模型能力声明支持时传入。

## 首次配置

缺少配置时，不要索要或接收对话中的 API Key。请让用户在自己的终端运行：

```bash
python3 "<skill-dir>/scripts/asynx.py" configure
```

正常配置只询问 API Key，并使用公共 Asynx 服务。只有用户明确说明是自托管部署时，才使用
`configure --base-url URL`。

## 单个任务

```bash
python3 "<skill-dir>/scripts/asynx.py" generate --prompt "<提示词>"
python3 "<skill-dir>/scripts/asynx.py" edit --prompt "<编辑指令>" --image "/absolute/path/input.png"
```

根据需要添加 `--model`、`--image-size`、`--aspect-ratio`、`--quality`、`--count`、`--output-format`、`--reference`、
`--mask` 和 `--output-dir`。脚本会创建异步 Task、轮询终态并下载所有输出 Asset。只有用户明确要求不等待时才使用 `--detach`。

## 批量任务

批量任务保存在本机 SQLite 中，Asynx 端每张图仍是独立 Task。创建批次后立即返回批次 ID，不要因为异步任务尚未完成而阻塞用户对话：

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

每个批次项都有稳定幂等键。进程中断、网络错误或 Agent 会话切换后，先调用：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch poll "<batch-id>"
```

不要重新提交已经存在的 Task。`batch poll` 会继续提交待处理项、查询已提交项并下载已完成 Asset。需要查看完整明细时添加 `--items`：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch status "<batch-id>" --items
```

用户询问“现在到哪了”“有哪些历史任务”时，先轮询活动批次，再根据需要运行 `batch status`、`batch list` 或 `history`。只有一个活动批次时可以省略批次 ID；有多个活动批次时先让用户选择。

支持暂停、恢复和取消：

```bash
python3 "<skill-dir>/scripts/asynx.py" batch pause "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch resume "<batch-id>"
python3 "<skill-dir>/scripts/asynx.py" batch cancel "<batch-id>"
```

批次单项失败不应使其他项失败。报告成功文件、失败项、Task ID 和结构化错误；不要无条件重试 `failed`、`timeout` 或 `canceled` 项。

## 结果与安全

脚本把状态日志写到 stderr，把一个 JSON 对象写到 stdout。成功后报告批次或 Task ID、实际模型、结果质量、计费金额和绝对文件路径；
Codex 桌面端用绝对路径展示生成的图片。生成文件默认在 `generated-images/<批次 ID>/`，不要把它们加入 Git，除非用户明确要求。
