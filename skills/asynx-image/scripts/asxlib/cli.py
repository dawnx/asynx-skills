from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, NoReturn

from .artifacts import recent_payload
from .batches import execute_batch
from .client import AsynxClient
from .config import (
    config_status,
    configure,
    doctor,
    legacy_installation_notice,
    load_credentials,
)
from .constants import (
    DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
    DEFAULT_OUTPUT_DIR,
    KNOWN_STATUSES,
    VERSION,
)
from .errors import AsxError
from .local_images import execute_local_image
from .output import emit, log
from .tasks import (
    cancel_local_task,
    history_payload,
    local_asset_payload,
    local_task_status,
    local_tasks_payload,
    models_payload,
    poll_local_tasks,
    recover_tasks,
    refresh_task,
    run_submission,
    task_payload,
    wait_and_download,
    wait_limit,
)


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if message.startswith("the following arguments are required:"):
            required = message.partition(":")[2].strip()
            description = f"缺少必填参数或命令：{required}。"
        elif message.startswith("argument "):
            argument = message.partition(":")[0].removeprefix("argument ")
            description = f"参数 {argument} 的值无效。"
        else:
            description = "命令参数无法识别或不完整。"
        raise AsxError(description + "请运行对应命令的 --help 查看用法。", code="invalid_arguments")


_OUTPUT_DIR_HELP = "结果保存目录；省略时保存到默认目录（可用配置项 output_dir 或 ASYNX_OUTPUT_DIR 调整）下按日期与描述命名的子目录"
_MAX_WAIT_HELP = "本次命令最多等待的秒数，到时返回仍在运行的任务而不取消；省略时等到任务截止，可用配置项 max_wait_seconds 设定默认值"
_QUOTE_HELP = "只向服务查询本次请求的价格，不提交任务、不产生费用"


def _add_extended_image_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--optimize-prompt-mode", choices=("standard", "fast"), help="提示词优化模式，仅在所选模型说明支持时使用")
    parser.add_argument("--params-file", help="图片高级参数 JSON 文件，显式命令参数优先")
    parser.add_argument("--svg-model", help="SVG 编写模型，默认 gpt-6.1-sol；仅需更换时指定")
    parser.add_argument("--svg-reasoning-effort", help="SVG 编写模型的推理强度；省略时不指定")
    parser.add_argument("--size", help="明确的像素宽x高，例如 2000x1000；1K/2K/4K 请用 --image-size，不自行换算")
    for name in ("complexity", "background", "moderation", "safety-identifier", "markup"):
        parser.add_argument("--" + name, help="图片参数 " + name)
    for name in ("output-compression", "frame-count", "frame-duration-ms"):
        parser.add_argument("--" + name, type=int, help="图片参数 " + name)


def _add_image_options(parser: argparse.ArgumentParser) -> None:
    _add_extended_image_options(parser)
    parser.add_argument("--prompt", required=True, help="生成或修改的内容描述")
    parser.add_argument("--model", help="模型名称或名称中唯一的片段，省略时使用默认模型")
    parser.add_argument("--image-size", help="尺寸档位，例如 1K、2K、4K；未指定时省略，由服务端采用默认设置")
    parser.add_argument("--aspect-ratio", help="画幅比例，例如 1:1、16:9")
    parser.add_argument("--quality", help="生成质量；GPT Image 2.5/2.6（按模型名识别）省略时为 high，其他模型及自定义模型名不补值；显式值优先")
    parser.add_argument("--count", type=int, default=1, help="本次生成数量，1–10 张")
    parser.add_argument("--output-format", choices=("png", "jpeg", "webp", "svg", "gif"), help="输出文件格式")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=_OUTPUT_DIR_HELP)
    parser.add_argument("--idempotency-key", help="自定义请求标识，用于重复提交时去重")
    parser.add_argument("--detach", action="store_true", help="提交后立即返回，之后通过 wait 等待结果")
    parser.add_argument("--max-wait", type=float, help=_MAX_WAIT_HELP)
    parser.add_argument("--quote", action="store_true", help=_QUOTE_HELP)
    parser.add_argument(
        "--keep-reference-original",
        action="store_true",
        help="参考图不重新编码；文件仍需满足上传限制",
    )


def _add_batch_creation_options(parser: argparse.ArgumentParser) -> None:
    _add_extended_image_options(parser)
    parser.add_argument(
        "--operation", choices=("generate", "edit", "video.generate"), default="generate", help="批次类型"
    )
    parser.add_argument("--prompt", help="提示词或编辑指令；所有任务使用同一描述时填写，与 --items-file 二选一")
    parser.add_argument(
        "--items-file",
        help="每项一条描述的清单：JSON Lines（每行一个字符串或含 prompt 的对象）或 JSON 数组；"
        "对象可覆盖 aspect_ratio、image_size、size、quality、count（视频为 duration、resolution、aspect_ratio）",
    )
    parser.add_argument("--quote", action="store_true", help="只查询整个批次的预计费用，不创建批次、不提交任务")
    parser.add_argument("--model", help="模型名称或唯一片段")
    parser.add_argument("--image-size", help="尺寸档位，例如 1K、2K、4K；未指定时省略，由服务端采用默认设置")
    parser.add_argument("--aspect-ratio", help="画幅比例，例如 1:1、16:9")
    parser.add_argument("--quality", help="生成质量；GPT Image 2.5/2.6（按模型名识别）省略时为 high，其他模型及自定义模型名不补值；显式值优先")
    parser.add_argument("--count", type=int, default=1, help="每个图片任务的输出数量")
    parser.add_argument(
        "--output-format",
        choices=("png", "jpeg", "webp", "svg", "gif"),
        help="输出格式",
    )
    parser.add_argument("--total", type=int, default=1, help="批次中的任务数量；使用 --items-file 时由清单决定")
    parser.add_argument("--output-dir", help="结果保存目录，默认在默认保存目录下为批次创建子目录")
    parser.add_argument("--name", help="批次名称")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
        help="本次最多提交的任务数",
    )
    parser.add_argument("--reference", action="append", default=[], help="参考图路径或 URL，可重复传入")
    parser.add_argument("--image", action="append", default=[], help="待修改的图片路径或 URL，可重复传入")
    parser.add_argument("--mask", help="编辑蒙版（Mask）图片")
    _add_video_input_options(parser)
    parser.add_argument(
        "--keep-reference-original",
        action="store_true",
        help="保留参考图原始格式；文件仍需满足上传限制",
    )


def _add_batch_add_options(parser: argparse.ArgumentParser) -> None:
    _add_extended_image_options(parser)
    parser.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    parser.add_argument("--total", type=int, default=1, help="追加的任务数量")
    parser.add_argument("--prompt", help="新的提示词，省略时沿用批次设置")
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--image-size", help="覆盖图片尺寸档位，例如 1K、2K、4K；省略时沿用原批次")
    parser.add_argument("--aspect-ratio", help="覆盖画幅比例")
    parser.add_argument("--quality", help="覆盖质量参数")
    parser.add_argument("--count", type=int, help="覆盖每个图片任务的输出数量")
    parser.add_argument(
        "--output-format", choices=("png", "jpeg", "webp", "svg", "gif"), help="覆盖输出格式"
    )
    parser.add_argument("--reference", action="append", help="新的参考图，可重复传入")
    parser.add_argument("--image", action="append", help="新的输入图片，可重复传入")
    parser.add_argument("--mask", help="覆盖编辑蒙版（Mask）图片")
    _add_video_input_options(parser)
    parser.add_argument(
        "--keep-reference-original",
        action="store_true",
        help="保留新传入且符合限制的参考图原始编码",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
        help="本次最多提交的任务数",
    )


def _add_video_input_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--duration", type=float, help="视频时长（秒），省略时使用 5 秒")
    parser.add_argument("--resolution", help="视频分辨率：720p、1080p 或 2K，默认 720p")
    parser.add_argument("--first-frame", help="首帧图片路径或 URL")
    parser.add_argument("--last-frame", help="尾帧图片路径或 URL，需同时提供首帧")
    parser.add_argument("--reference-video", action="append", help="参考视频的 HTTP(S) 链接，可重复传入")
    parser.add_argument("--reference-audio", action="append", help="参考音频的 HTTP(S) 链接，可重复传入")


def _add_local_output_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--output", required=True, help="输出文件路径")
    parser.add_argument("--format", choices=("png", "jpeg", "jpg", "webp"), help="输出格式，默认按输出文件名后缀推断")


def _add_local_image_commands(commands: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    image = commands.add_parser("image", help="在本机处理图片，不需要 API Key")
    image_commands = image.add_subparsers(dest="image_command", required=True)

    info = image_commands.add_parser("info", help="查看本机图片信息")
    info.add_argument("input", help="图片文件路径")

    convert = image_commands.add_parser("convert", help="转换图片格式")
    convert.add_argument("input")
    _add_local_output_options(convert)

    resize = image_commands.add_parser("resize", help="按宽度或高度缩放图片")
    resize.add_argument("input")
    _add_local_output_options(resize)
    resize.add_argument("--width", type=int, help="目标宽度（像素）")
    resize.add_argument("--height", type=int, help="目标高度（像素）")

    crop = image_commands.add_parser("crop", help="按坐标裁剪图片")
    crop.add_argument("input")
    _add_local_output_options(crop)
    crop.add_argument("--box", required=True, help="left,top,right,bottom")

    slice_command = image_commands.add_parser("slice", help="按行列网格切图")
    slice_command.add_argument("input")
    slice_command.add_argument("--output-dir", required=True, help="切片保存目录")
    slice_command.add_argument("--rows", type=int, required=True, help="切分行数")
    slice_command.add_argument("--columns", type=int, required=True, help="切分列数")
    slice_command.add_argument("--prefix", default="tile", help="切片文件名前缀")
    slice_command.add_argument("--format", choices=("png", "jpeg", "jpg", "webp"))

    sheet = image_commands.add_parser("contact-sheet", help="将多张图片拼接为预览图")
    sheet.add_argument("inputs", nargs="+")
    _add_local_output_options(sheet)
    sheet.add_argument("--columns", type=int, default=4, help="每行排列的图片数量")
    sheet.add_argument("--cell-width", type=int, default=320, help="每格宽度（像素）")
    sheet.add_argument("--cell-height", type=int, default=320, help="每格高度（像素）")
    sheet.add_argument("--background", default="#ffffff", help="背景色，例如 #ffffff")

    mask = image_commands.add_parser("apply-mask", help="按蒙版将指定区域置为透明")
    mask.add_argument("input")
    mask.add_argument("--mask", required=True)
    _add_local_output_options(mask)

    batch_convert = image_commands.add_parser("batch-convert", help="批量转换目录中的图片")
    batch_convert.add_argument("--input-dir", required=True, help="待转换图片所在目录")
    batch_convert.add_argument("--output-dir", required=True, help="转换后的保存目录")
    batch_convert.add_argument("--format", choices=("png", "jpeg", "jpg", "webp"), required=True)
    batch_convert.add_argument("--recursive", action="store_true", help="同时处理子目录")


def parser() -> argparse.ArgumentParser:
    result: argparse.ArgumentParser = _ArgumentParser(description="通过 Asynx 生成图片、视频和 3D 模型")
    result.add_argument("--version", action="version", version=VERSION)
    commands = result.add_subparsers(dest="command", required=True)
    _add_local_image_commands(commands)

    configure_command = commands.add_parser(
        "configure", help="在本机配置 API Key"
    )
    configure_command.add_argument(
        "--base-url",
        help="自托管 Asynx 服务地址；使用公共服务时无需设置",
    )
    update = commands.add_parser("update", help="检查、安装或回滚 Skill 更新")
    update_commands = update.add_subparsers(dest="update_command", required=True)
    update_commands.add_parser("status", help="离线查看版本、安装位置和上次检查结果")
    check = update_commands.add_parser("check", help="检查是否有新版本")
    check.add_argument("--force", action="store_true", help="立即联网检查，忽略缓存")
    apply = update_commands.add_parser("apply", help="升级已安装的副本，并保留旧版本备份")
    apply.add_argument("--target", choices=("all", "current", "codex", "claude"), default="all", help="更新范围，默认更新所有由本工具管理的已安装副本")
    apply.add_argument("--force", action="store_true", help="备份后覆盖安装目录中的本地改动")
    update_commands.add_parser("rollback", help="恢复到上一次更新前的版本，或恢复中断的更新")
    config = commands.add_parser("config", help="查看本机 Asynx 配置")
    config_commands = config.add_subparsers(dest="config_command", required=True)
    config_commands.add_parser("status", help="查看配置状态，不会显示完整 API Key")

    doctor_command = commands.add_parser("doctor", help="检查配置和 Skill 安装状态")
    doctor_command.add_argument(
        "--verify",
        action="store_true",
        help="验证密钥并读取模型列表，不提交生成任务",
    )
    doctor_command.add_argument("--updates", action="store_true", help="同时检查 Skill 更新")
    models = commands.add_parser("models", help="列出可用模型，默认仅列出图片模型")
    models.add_argument("--operation", choices=("all", "generate", "edit"), default="all", help="按用途筛选")
    models.add_argument("--modality", choices=("image", "video", "model", "all"), default="image", help="图片模型、视频模型、3D 模型（model）或全部")
    model_commands = models.add_subparsers(dest="models_command")
    describe = model_commands.add_parser("describe", help="读取当前账户可用的模型说明")
    describe.add_argument("--model", required=True, help="模型名称或别名")
    describe.add_argument("--examples", action="store_true", help="附带服务生成的调用示例，默认仅返回能力与使用说明")

    generate = commands.add_parser("generate", help="生成一张或多张新图片")
    _add_image_options(generate)
    generate.add_argument("--reference", action="append", default=[], help="参考图路径或 URL，可重复传入")

    edit = commands.add_parser("edit", help="修改已有图片")
    _add_image_options(edit)
    edit.add_argument("--image", action="append", default=[], help="待修改的图片路径或 URL，可重复传入")
    edit.add_argument("--from-task", help="使用本机任务记录中该任务已下载的图片")
    edit.add_argument("--mask", help="编辑蒙版（Mask）图片路径或 URL")

    video = commands.add_parser("video", help="生成视频")
    video_commands = video.add_subparsers(dest="video_command", required=True)
    video_generate = video_commands.add_parser("generate", help="生成并下载视频")
    _add_video_input_options(video_generate)
    video_generate.add_argument("--prompt", required=True)
    video_generate.add_argument("--model", help="视频模型名称，省略时使用默认模型")
    video_generate.add_argument("--aspect-ratio", help="画幅比例，默认 16:9")
    video_generate.add_argument("--reference", action="append", default=[], help="参考图路径或 URL，可重复传入")
    video_generate.add_argument("--from-task", help="将该任务唯一一张本地图片用作首帧")
    video_generate.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=_OUTPUT_DIR_HELP)
    video_generate.add_argument("--idempotency-key", help="自定义请求标识，用于重复提交时去重")
    video_generate.add_argument("--detach", action="store_true", help="提交后立即返回，之后通过 wait 等待结果")
    video_generate.add_argument("--max-wait", type=float, help=_MAX_WAIT_HELP)
    video_generate.add_argument("--quote", action="store_true", help=_QUOTE_HELP)

    model = commands.add_parser("model", help="生成 3D 模型")
    model_commands_3d = model.add_subparsers(dest="model_command", required=True)
    model_generate = model_commands_3d.add_parser("generate", help="生成并下载 GLB 3D 模型")
    model_generate.add_argument("--prompt", help="文字生成的模型描述，最多 1024 个字符；与 --image 二选一")
    model_generate.add_argument("--image", help="图片生成的参考图，本地路径或 HTTP(S) 链接，仅一张；与 --prompt 二选一")
    model_generate.add_argument("--from-task", help="将该任务唯一一张本地图片用作参考图")
    model_generate.add_argument("--negative-prompt", help="不希望出现的内容，仅文字生成可用，最多 255 个字符")
    model_generate.add_argument("--no-texture", action="store_true", help="不生成贴图，仅输出几何体")
    model_generate.add_argument("--no-pbr", action="store_true", help="不生成 PBR 材质")
    model_generate.add_argument("--max-faces", type=int, help="面数上限（1–2000000），实际面数可能更少")
    model_generate.add_argument("--seed", type=int, help="几何随机种子")
    model_generate.add_argument("--texture-seed", type=int, help="贴图随机种子")
    model_generate.add_argument("--model", help="3D 模型名称；账户仅有一个可用 3D 模型时可省略")
    model_generate.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help=_OUTPUT_DIR_HELP)
    model_generate.add_argument("--idempotency-key", help="自定义请求标识，用于重复提交时去重")
    model_generate.add_argument("--detach", action="store_true", help="提交后立即返回，之后通过 wait 等待结果")
    model_generate.add_argument("--max-wait", type=float, help=_MAX_WAIT_HELP)
    model_generate.add_argument("--quote", action="store_true", help=_QUOTE_HELP)

    status = commands.add_parser("status", help="查询任务在服务端的最新状态")
    status.add_argument("task_id", help="服务端任务 ID")

    wait = commands.add_parser("wait", help="等待任务完成并下载结果")
    wait.add_argument("task_id", help="本地任务 ID 或服务端任务 ID")
    wait.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="结果保存目录；省略时沿用该任务提交时的目录")
    wait.add_argument("--max-wait", type=float, help=_MAX_WAIT_HELP)

    history = commands.add_parser("history", help="查询服务端历史任务")
    history.add_argument("--status", choices=tuple(sorted(KNOWN_STATUSES)), help="按状态筛选")
    history.add_argument("--model", help="按模型筛选")
    history.add_argument("--limit", type=int, default=20, help="返回条数")

    recent = commands.add_parser("recent", help="查看本机已下载的生成结果")
    recent.add_argument("--query", help="按任务 ID、提示词、模型或文件名筛选")
    recent.add_argument("--limit", type=int, default=20, help="返回条数")
    recent.add_argument("--latest", action="store_true", help="仅返回最近一个结果")
    recent.add_argument("--modality", choices=("image", "video", "model"), help="仅返回图片、视频或 3D 模型")

    task = commands.add_parser("task", help="管理本机任务记录")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_list = task_commands.add_parser("list", help="列出本机任务")
    task_list.add_argument("--status", help="按本机记录的状态筛选")
    task_list.add_argument("--limit", type=int, default=50, help="返回条数")
    task_status = task_commands.add_parser("status", help="查看本机保存的任务状态")
    task_status.add_argument("task_id")
    task_status.add_argument("--refresh", action="store_true", help="查询最新进度并下载已完成的结果，不提交任务")
    task_poll = task_commands.add_parser("poll", help="刷新一个或全部本机任务")
    task_poll.add_argument("task_id", nargs="?", help="仅刷新指定任务；省略时按 --limit 刷新多个")
    task_poll.add_argument("--limit", type=int, default=50, help="最多刷新的任务数")
    task_recover = task_commands.add_parser("recover", help="确认提交中断的原任务，可能补提交请求")
    task_recover.add_argument("task_id", nargs="?", help="仅处理指定任务；省略时检查最近一批记录")
    task_recover.add_argument("--limit", type=int, default=50, help="未指定 ID 时最多检查的任务数")
    task_recover.add_argument(
        "--max-age-hours", type=float, default=24,
        help="未指定 ID 时，只重新提交这段时间内创建的未确认记录；更早的仅列出，不自动提交",
    )
    task_cancel = task_commands.add_parser("cancel", help="请求取消服务端的任务")
    task_cancel.add_argument("task_id")

    prune = commands.add_parser("prune", help="精简本机任务记录，不删除任何结果文件")
    prune.add_argument(
        "--older-than-days", type=int,
        help="同时删除这些天之前已结束的任务与批次记录；省略时只移除已结束任务中内嵌的参考图数据",
    )
    prune.add_argument("--dry-run", action="store_true", help="只统计将精简的内容，不做任何修改")

    asset = commands.add_parser("asset", help="查看本机结果文件")
    asset_commands = asset.add_subparsers(dest="asset_command", required=True)
    asset_list = asset_commands.add_parser("list", help="列出指定任务的结果文件")
    asset_list.add_argument("task_id")

    batch = commands.add_parser("batch", help="管理可分次推进的批次")
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    create = batch_commands.add_parser("create", help="创建批次并开始提交任务")
    _add_batch_creation_options(create)
    add = batch_commands.add_parser("add", help="向批次追加任务")
    _add_batch_add_options(add)

    poll = batch_commands.add_parser("poll", help="刷新进度并提交尚未提交的项目")
    poll.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    poll.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
        help="本次最多提交的任务数",
    )

    batch_wait = batch_commands.add_parser("wait", help="继续提交剩余项目并等待结果")
    batch_wait.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    batch_wait.add_argument("--interval", type=float, default=10, help="轮询间隔秒数")
    batch_wait.add_argument("--max-wait", type=float, help="最长等待秒数，0 表示不限时；省略时使用配置项 max_wait_seconds（默认不限时）")

    batch_status = batch_commands.add_parser("status", help="查看批次进度")
    batch_status.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    batch_status.add_argument("--refresh", action="store_true", help="刷新已提交任务并下载结果，不提交新任务")
    batch_status.add_argument("--items", action="store_true", help="列出每个任务的明细")
    batch_commands.add_parser("list", help="列出本机批次")

    pause = batch_commands.add_parser("pause", help="暂停提交新任务，已提交任务继续执行")
    pause.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    resume = batch_commands.add_parser("resume", help="恢复批次提交")
    resume.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    cancel = batch_commands.add_parser("cancel", help="请求取消批次任务，已开始的任务可能产生费用")
    cancel.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    return result


def _client() -> AsynxClient:
    api_key, base_url = load_credentials()
    return AsynxClient(base_url, api_key)


def execute(args: argparse.Namespace, *, locked_root: Path | None = None) -> tuple[dict[str, Any], int]:
    if args.command == "update":
        from .updates import update_apply, update_check, update_rollback, update_status

        if args.update_command == "status":
            return update_status(), 0
        if args.update_command == "check":
            checked = update_check(force=args.force)
            return checked, 0 if checked["ok"] else 2
        if args.update_command == "apply":
            return update_apply(target=args.target, force=args.force, already_locked=locked_root), 0
        return update_rollback(already_locked=locked_root), 0
    if args.command == "image":
        return execute_local_image(args)
    if args.command == "configure":
        return configure(args.base_url), 0
    if args.command == "config" and args.config_command == "status":
        return config_status(), 0
    if args.command == "doctor":
        payload = doctor(verify=args.verify)
        if args.updates:
            from .updates import update_check

            payload["updates"] = update_check()
        return payload, 0 if payload["ok"] else 2
    if args.command == "recent":
        return recent_payload(args.query, args.limit, latest=args.latest, modality=args.modality), 0
    if args.command == "prune":
        from .maintenance import prune_state

        return prune_state(older_than_days=args.older_than_days, dry_run=args.dry_run), 0
    if args.command == "task":
        if args.task_command == "list":
            return local_tasks_payload(status=args.status, limit=args.limit), 0
        if args.task_command == "status":
            if args.refresh:
                result, code = refresh_task(_client(), args.task_id)
                return {"ok": result["ok"], "schema_version": 1, "task": result}, code
            return local_task_status(args.task_id), 0
        client = _client()
        if args.task_command == "poll":
            return poll_local_tasks(client, args.task_id, limit=args.limit), 0
        if args.task_command == "recover":
            result = recover_tasks(client, args.task_id, limit=args.limit, max_age_hours=args.max_age_hours)
            return result, 0 if result["ok"] else 4
        if args.task_command == "cancel":
            return cancel_local_task(client, args.task_id)
    if args.command == "asset" and args.asset_command == "list":
        return local_asset_payload(args.task_id), 0
    if (
        args.command == "batch"
        and args.batch_command in {"list", "status"}
        and not getattr(args, "refresh", False)
    ):
        return execute_batch(None, args)
    client = _client()
    if args.command == "models":
        if args.models_command == "describe":
            guide, request_id = client.model_guide(args.model, examples=args.examples)
            payload = {"ok": True, **guide}
            if request_id:
                payload["request_id"] = request_id
            return payload, 0
        return models_payload(client, args.operation, modality=args.modality), 0
    if args.command == "video":
        return run_submission(client, args, "video.generate")
    if args.command == "model":
        return run_submission(client, args, "model.generate")
    if args.command == "generate":
        return run_submission(client, args, "image.generate")
    if args.command == "edit":
        return run_submission(client, args, "image.edit")
    if args.command == "status":
        task, request_id = client.task(args.task_id)
        return task_payload(task, request_id), 0
    if args.command == "wait":
        return wait_and_download(client, args.task_id, args.output_dir, max_wait=wait_limit(args))
    if args.command == "history":
        return history_payload(client, status=args.status, model=args.model, limit=args.limit), 0
    if args.command == "batch":
        return execute_batch(client, args)
    raise AssertionError("unreachable")


def main(*, locked_root: Path | None = None) -> NoReturn:
    args: argparse.Namespace | None = None
    try:
        args = parser().parse_args()
        payload, exit_code = execute(args, locked_root=locked_root)
        if exit_code == 0 and payload.get("ok") and (
            (args.command in {"generate", "edit", "video", "model", "wait"} and payload.get("status") == "succeeded")
            or (args.command == "batch" and payload.get("batch", {}).get("status") == "completed")
        ):
            from .updates import automatic_update_notice

            notice = automatic_update_notice()
            if notice:
                payload["update_notice"] = notice
    except AsxError as exc:
        payload, exit_code = exc.payload(), exc.exit_code
    except KeyboardInterrupt:
        payload, exit_code = {
            "ok": False,
            "error": {"code": "interrupted", "message": "操作已中断，已提交的任务不会因此取消。请查询或恢复原任务，避免重复生成。"},
        }, 130
    except (OSError, sqlite3.Error):
        payload, exit_code = AsxError(
            "本地文件或任务记录读写失败，请检查目录权限和剩余空间；若任务已提交，请先确认原任务状态。",
            code="state_io_error",
            exit_code=2,
        ).payload(), 2
    except Exception:  # noqa: BLE001 - CLI must always emit one JSON object
        payload, exit_code = AsxError(
            "操作未能完成。请保留任务 ID 并检查 Skill 版本，必要时联系服务支持；若任务已提交，请勿直接重复生成。", code="unexpected_error", exit_code=2
        ).payload(), 2
    legacy_notice = legacy_installation_notice()
    if legacy_notice:
        payload["legacy_installation_notice"] = legacy_notice
    if args is not None and args.command == "configure" and sys.stdout.isatty():
        # configure is the one CLI interaction users perform themselves.
        # Keep redirected stdout JSON for callers that consume its existing contract.
        if not payload.get("ok"):
            log(payload["error"]["message"])
        if legacy_notice:
            log("检测到旧版 asx 入口，可让 Agent 协助检查并清理；密钥和任务记录需保留。")
    else:
        emit(payload)
    raise SystemExit(exit_code)
