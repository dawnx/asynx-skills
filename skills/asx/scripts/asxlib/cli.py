from __future__ import annotations

import argparse
import sqlite3
from typing import Any, NoReturn

from .artifacts import recent_payload
from .batches import execute_batch
from .client import AsynxClient
from .config import configure, load_credentials
from .constants import (
    DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
    DEFAULT_OUTPUT_DIR,
    KNOWN_STATUSES,
    VERSION,
)
from .errors import AsxError
from .output import emit
from .tasks import (
    cancel_local_task,
    history_payload,
    local_asset_payload,
    local_task_status,
    local_tasks_payload,
    models_payload,
    poll_local_tasks,
    recover_tasks,
    run_submission,
    task_payload,
    wait_and_download,
)


def _add_image_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt", required=True)
    parser.add_argument("--model")
    parser.add_argument("--image-size", default="1K")
    parser.add_argument("--aspect-ratio", default="1:1")
    parser.add_argument("--quality", default="standard")
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--output-format", choices=("png", "jpeg", "webp"), default="png")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--idempotency-key")
    parser.add_argument("--detach", action="store_true")
    parser.add_argument(
        "--keep-reference-original",
        action="store_true",
        help="保留符合限制的参考图原始编码，不进行 WebP 归一化",
    )


def _add_batch_creation_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--operation", choices=("generate", "edit"), default="generate", help="批次类型"
    )
    parser.add_argument("--prompt", required=True, help="提示词或编辑指令")
    parser.add_argument("--model", help="模型名称或唯一片段")
    parser.add_argument("--image-size", default="1K", help="图片尺寸档位")
    parser.add_argument("--aspect-ratio", default="1:1", help="宽高比")
    parser.add_argument("--quality", default="standard", help="质量参数")
    parser.add_argument("--count", type=int, default=1, help="每个 Task 的输出数量")
    parser.add_argument(
        "--output-format",
        choices=("png", "jpeg", "webp"),
        default="png",
        help="输出格式",
    )
    parser.add_argument("--total", type=int, default=1, help="批次中的 Task 数量")
    parser.add_argument("--output-dir", help="输出目录，默认 generated-images/<批次 ID>")
    parser.add_argument("--name", help="批次名称")
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
        help="本次最多提交的 Task 数",
    )
    parser.add_argument("--reference", action="append", default=[], help="生成参考图，可重复")
    parser.add_argument("--image", action="append", default=[], help="编辑输入图，可重复")
    parser.add_argument("--mask", help="编辑 Mask 图片")
    parser.add_argument(
        "--keep-reference-original",
        action="store_true",
        help="保留符合限制的参考图原始编码，不进行 WebP 归一化",
    )


def _add_batch_add_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    parser.add_argument("--total", type=int, default=1, help="追加的 Task 数量")
    parser.add_argument("--prompt", help="新的提示词；省略时沿用批次设置")
    parser.add_argument("--model", help=argparse.SUPPRESS)
    parser.add_argument("--image-size", help="覆盖图片尺寸档位")
    parser.add_argument("--aspect-ratio", help="覆盖宽高比")
    parser.add_argument("--quality", help="覆盖质量参数")
    parser.add_argument("--count", type=int, help="覆盖每个 Task 的输出数量")
    parser.add_argument(
        "--output-format", choices=("png", "jpeg", "webp"), help="覆盖输出格式"
    )
    parser.add_argument("--reference", action="append", help="生成参考图，可重复")
    parser.add_argument("--image", action="append", help="编辑输入图，可重复")
    parser.add_argument("--mask", help="覆盖编辑 Mask 图片")
    parser.add_argument(
        "--keep-reference-original",
        action="store_true",
        help="保留新传入且符合限制的参考图原始编码",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
        help="本次最多提交的 Task 数",
    )


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="通过 Asynx 生成和编辑图片")
    result.add_argument("--version", action="version", version=VERSION)
    commands = result.add_subparsers(dest="command", required=True)

    configure_command = commands.add_parser("configure", help="安全保存 Asynx API Key")
    configure_command.add_argument(
        "--base-url",
        help="use a self-hosted Asynx API instead of the default service",
    )
    models = commands.add_parser("models", help="列出可用图片模型")
    models.add_argument("--operation", choices=("all", "generate", "edit"), default="all")

    generate = commands.add_parser("generate", help="生成一张或多张图片")
    _add_image_options(generate)
    generate.add_argument("--reference", action="append", default=[])

    edit = commands.add_parser("edit", help="编辑已有图片")
    _add_image_options(edit)
    edit.add_argument("--image", action="append", default=[])
    edit.add_argument("--from-task", help="使用本机索引中指定 Task 的已下载图片")
    edit.add_argument("--mask")

    status = commands.add_parser("status", help="查询一个 Task")
    status.add_argument("task_id")

    wait = commands.add_parser("wait", help="等待并下载一个 Task")
    wait.add_argument("task_id")
    wait.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)

    history = commands.add_parser("history", help="查询历史 Task")
    history.add_argument("--status", choices=tuple(sorted(KNOWN_STATUSES)), help="按状态筛选")
    history.add_argument("--model", help="按模型筛选")
    history.add_argument("--limit", type=int, default=20, help="返回数量")

    recent = commands.add_parser("recent", help="查询本机已下载的生成结果")
    recent.add_argument("--query", help="按 Task ID、Prompt、模型或文件名筛选")
    recent.add_argument("--limit", type=int, default=20, help="返回数量")
    recent.add_argument("--latest", action="store_true", help="只返回最近一个结果")

    task = commands.add_parser("task", help="管理本地任务账本")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_list = task_commands.add_parser("list", help="列出本地任务")
    task_list.add_argument("--status", help="按本地状态筛选")
    task_list.add_argument("--limit", type=int, default=50, help="返回数量")
    task_status = task_commands.add_parser("status", help="查看本地任务快照")
    task_status.add_argument("task_id")
    task_poll = task_commands.add_parser("poll", help="刷新一个或全部本地任务")
    task_poll.add_argument("task_id", nargs="?")
    task_poll.add_argument("--limit", type=int, default=50, help="最多刷新任务数")
    task_commands.add_parser("recover", help="恢复未完成或提交窗口中断的任务").add_argument(
        "--limit", type=int, default=50, help="最多恢复任务数"
    )
    task_cancel = task_commands.add_parser("cancel", help="请求取消远端任务")
    task_cancel.add_argument("task_id")

    asset = commands.add_parser("asset", help="查看本地 Asset")
    asset_commands = asset.add_subparsers(dest="asset_command", required=True)
    asset_list = asset_commands.add_parser("list", help="列出任务输出 Asset")
    asset_list.add_argument("task_id")

    batch = commands.add_parser("batch", help="管理可恢复的本地批次")
    batch_commands = batch.add_subparsers(dest="batch_command", required=True)
    create = batch_commands.add_parser("create", help="创建并启动批次")
    _add_batch_creation_options(create)
    add = batch_commands.add_parser("add", help="向批次追加 Task")
    _add_batch_add_options(add)

    poll = batch_commands.add_parser("poll", help="推进一次批次轮询")
    poll.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    poll.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_BATCH_SUBMISSIONS_PER_POLL,
        help="本次最多提交的 Task 数",
    )

    batch_wait = batch_commands.add_parser("wait", help="持续轮询批次直到完成")
    batch_wait.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    batch_wait.add_argument("--interval", type=float, default=10, help="轮询间隔秒数")
    batch_wait.add_argument("--max-wait", type=float, default=0, help="最多等待秒数，0 表示不限制")

    batch_status = batch_commands.add_parser("status", help="查询批次状态")
    batch_status.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    batch_status.add_argument("--refresh", action="store_true", help="查询 Asynx 最新状态")
    batch_status.add_argument("--items", action="store_true", help="列出每个 Task")
    batch_commands.add_parser("list", help="列出本机批次")

    pause = batch_commands.add_parser("pause", help="暂停提交新 Task")
    pause.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    resume = batch_commands.add_parser("resume", help="恢复批次提交")
    resume.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    cancel = batch_commands.add_parser("cancel", help="取消批次中的 Task")
    cancel.add_argument("batch_id", nargs="?", help="批次 ID，省略时使用最近的活动批次")
    return result


def _client() -> AsynxClient:
    api_key, base_url = load_credentials()
    return AsynxClient(base_url, api_key)


def execute(args: argparse.Namespace) -> tuple[dict[str, Any], int]:
    if args.command == "configure":
        return configure(args.base_url), 0
    if args.command == "recent":
        return recent_payload(args.query, args.limit, latest=args.latest), 0
    if args.command == "task":
        if args.task_command == "list":
            return local_tasks_payload(status=args.status, limit=args.limit), 0
        if args.task_command == "status":
            return local_task_status(args.task_id), 0
        client = _client()
        if args.task_command == "poll":
            return poll_local_tasks(client, args.task_id, limit=args.limit), 0
        if args.task_command == "recover":
            return recover_tasks(client, limit=args.limit), 0
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
        return models_payload(client, args.operation), 0
    if args.command == "generate":
        return run_submission(client, args, "image.generate")
    if args.command == "edit":
        return run_submission(client, args, "image.edit")
    if args.command == "status":
        task, request_id = client.task(args.task_id)
        return task_payload(task, request_id), 0
    if args.command == "wait":
        return wait_and_download(client, args.task_id, args.output_dir)
    if args.command == "history":
        return history_payload(client, status=args.status, model=args.model, limit=args.limit), 0
    if args.command == "batch":
        return execute_batch(client, args)
    raise AssertionError("unreachable")


def main() -> NoReturn:
    try:
        payload, exit_code = execute(parser().parse_args())
    except AsxError as exc:
        payload, exit_code = exc.payload(), exc.exit_code
    except KeyboardInterrupt:
        payload, exit_code = {
            "ok": False,
            "error": {"code": "interrupted", "message": "Operation interrupted"},
        }, 130
    except (OSError, sqlite3.Error) as exc:
        payload, exit_code = AsxError(
            f"本地状态读写失败：{exc}",
            code="state_io_error",
            exit_code=2,
        ).payload(), 2
    except Exception as exc:  # noqa: BLE001 - CLI must always emit one JSON object
        payload, exit_code = AsxError(
            f"操作失败：{exc}", code="unexpected_error", exit_code=2
        ).payload(), 2
    emit(payload)
    raise SystemExit(exit_code)
