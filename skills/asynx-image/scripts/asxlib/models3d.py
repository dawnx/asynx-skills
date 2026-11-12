"""3D model requests use the public model.generate contract, never provider fields."""
from __future__ import annotations

import argparse
from typing import Any

from .errors import AsxError
from .images import _cached_models, _looks_canonical, cache_models, resolve_model
from .reference_media import prepare_inputs, validate_request_body

MODEL_TASK_TYPE = "model.generate"
MAX_MODEL_PROMPT_CHARACTERS = 1024
MAX_MODEL_NEGATIVE_PROMPT_CHARACTERS = 255
MAX_MODEL_FACES = 2_000_000
MAX_MODEL_SEED = 2**31 - 1
_LIST_MODELS_ARGS = ["models", "--modality", "model"]
_LIST_MODELS_HINT = "可运行 models --modality model 查看可用的 3D 模型。"


def _generation_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        model for model in models
        if MODEL_TASK_TYPE in model.get("task_types", []) and isinstance(model.get("name"), str)
    ]


def resolve_generation_model(client: Any, selector: str | None) -> tuple[str, str | None]:
    """Pick the 3D model without a built-in default: catalog names are operator-defined."""
    scope = getattr(client, "cache_scope", None)
    models = _cached_models(client.base_url, scope=scope)
    request_id: str | None = None
    requested = selector.strip() if selector is not None else None
    if requested is not None:
        if not 1 <= len(requested) <= 160:
            raise AsxError("模型名称须为 1–160 个字符，请检查名称是否正确。", code="invalid_model")
        exact = [
            model for model in _generation_models(models or [])
            if str(model["name"]).casefold() == requested.casefold()
        ]
        if len(exact) == 1:
            return str(exact[0]["name"]), None
        if _looks_canonical(requested):
            return requested, None
    # A catalog cached before 3D models were listed must not hide them.
    if models is None or not _generation_models(models):
        models, request_id = client.models()
        cache_models(client.base_url, models, scope=scope)
    available = [str(model["name"]) for model in _generation_models(models)]
    if not available:
        raise AsxError(
            "当前账户没有可用的 3D 模型，暂时无法生成。" + _LIST_MODELS_HINT,
            code="no_compatible_model",
            details={"next_action_args": _LIST_MODELS_ARGS},
        )
    if requested is not None:
        return str(resolve_model(models, requested, MODEL_TASK_TYPE)["name"]), request_id
    if len(available) == 1:
        return available[0], request_id
    raise AsxError(
        f"当前账户有 {len(available)} 个可用的 3D 模型，请通过 --model 指定其中一个。" + _LIST_MODELS_HINT,
        code="model_required",
        details={"available": available, "next_action_args": _LIST_MODELS_ARGS},
    )


def _bounded_integer(value: Any, *, minimum: int, maximum: int, message: str, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise AsxError(message, code=code)
    return value


def build_model_task(
    client: Any, *, prompt: str | None = None, image: str | None = None,
    negative_prompt: str | None = None, model_selector: str | None = None,
    texture: bool = True, pbr: bool = True, max_faces: int | None = None,
    seed: int | None = None, texture_seed: int | None = None,
) -> tuple[dict[str, Any], str, str | None]:
    if prompt is not None and image is not None:
        raise AsxError(
            "--prompt 与 --image 不能同时使用：文字生成只接受描述，图片生成只接受一张参考图。",
            code="conflicting_model_inputs",
        )
    if prompt is None and image is None:
        raise AsxError(
            "请通过 --prompt 提供文字描述，或通过 --image 提供一张参考图，二者选其一。",
            code="missing_model_input",
        )
    task_input: dict[str, Any]
    if image is not None:
        if negative_prompt is not None:
            raise AsxError("--negative-prompt 仅用于文字生成，不能与参考图同时使用。", code="conflicting_model_inputs")
        task_input = {"mode": "image"}
    else:
        text = prompt if prompt is not None else ""
        if not text.strip() or len(text) > MAX_MODEL_PROMPT_CHARACTERS:
            raise AsxError(f"3D 模型描述须为 1–{MAX_MODEL_PROMPT_CHARACTERS} 个字符。", code="invalid_prompt")
        task_input = {"mode": "text", "prompt": text}
        if negative_prompt is not None:
            if not 1 <= len(negative_prompt) <= MAX_MODEL_NEGATIVE_PROMPT_CHARACTERS:
                raise AsxError(
                    f"排除内容描述须为 1–{MAX_MODEL_NEGATIVE_PROMPT_CHARACTERS} 个字符；无需排除时请省略该参数。",
                    code="invalid_negative_prompt",
                )
            task_input["negative_prompt"] = negative_prompt
    texture_options: dict[str, Any] = {}
    if not texture:
        if texture_seed is not None:
            raise AsxError("--texture-seed 仅在生成贴图时有效，不能与 --no-texture 同时使用。", code="conflicting_model_inputs")
        texture_options["enabled"] = False
    if not pbr:
        texture_options["pbr"] = False
    seed_message = f"随机种子须为 0–{MAX_MODEL_SEED} 的整数。"
    if texture_seed is not None:
        texture_options["seed"] = _bounded_integer(
            texture_seed, minimum=0, maximum=MAX_MODEL_SEED, message=seed_message, code="invalid_model_seed"
        )
    geometry: dict[str, Any] = {}
    if max_faces is not None:
        geometry["max_faces"] = _bounded_integer(
            max_faces, minimum=1, maximum=MAX_MODEL_FACES,
            message=f"面数上限须为 1–{MAX_MODEL_FACES} 的整数。", code="invalid_max_faces",
        )
    if seed is not None:
        geometry["seed"] = _bounded_integer(
            seed, minimum=0, maximum=MAX_MODEL_SEED, message=seed_message, code="invalid_model_seed"
        )
    if image is not None:
        # Same limits and encoding as image references; URLs are passed through.
        prepared, _mask = prepare_inputs([image], mask=None, keep_original=False)
        task_input["image"] = prepared[0]
    if texture_options:
        task_input["texture"] = texture_options
    if geometry:
        task_input["geometry"] = geometry
    # GLB is the only format this client can verify after download.
    task_input["output"] = {"formats": ["glb"]}
    model, request_id = resolve_generation_model(client, model_selector)
    body = {"task_type": MODEL_TASK_TYPE, "model": model, "asset_delivery": "asynx", "input": task_input}
    validate_request_body(body)
    return body, model, request_id


def body_from_model_args(client: Any, args: argparse.Namespace) -> tuple[dict[str, Any], str, str | None]:
    return build_model_task(
        client, prompt=getattr(args, "prompt", None), image=getattr(args, "image", None),
        negative_prompt=getattr(args, "negative_prompt", None), model_selector=getattr(args, "model", None),
        texture=not getattr(args, "no_texture", False), pbr=not getattr(args, "no_pbr", False),
        max_faces=getattr(args, "max_faces", None), seed=getattr(args, "seed", None),
        texture_seed=getattr(args, "texture_seed", None),
    )
