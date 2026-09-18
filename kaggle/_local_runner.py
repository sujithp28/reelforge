"""Shared diffusers pipeline loading for local Kaggle GPU workers.

Every local-model worker (wan_worker.py, reelforge_worker.py, and any future
one) loads a diffusers pipeline the same way: fp16 on a T4 (no bf16
hardware), CPU offload, attention slicing, VAE tiling/slicing, plus any
per-submodule dtype casts a specific model needs.

That last part exists because `from_pretrained(torch_dtype=...)` does not
reliably reach every submodule - verified on real Kaggle T4 hardware, where
Wan's transformer silently stayed in its checkpoint dtype unless cast
explicitly after load. `dtype_fixups` makes that fix reusable instead of
something only Wan's runner remembers to do.

Adding a new local model means calling `load_pipeline()` with its pipeline
class name and any fixups it needs - not re-deriving the memory-fit dance
from scratch in a new Runner class.
"""
from __future__ import annotations

import logging

log = logging.getLogger("reelforge.local_runner")


def load_pipeline(
    pipeline_class: str,
    model_id: str,
    device: str = "0",
    dtype_fixups: dict[str, "object"] | None = None,
):
    """Load `pipeline_class` from `model_id` with T4-safe memory settings.

    Args:
        pipeline_class: name of a diffusers pipeline class, e.g. "WanPipeline".
        model_id: Hugging Face repo id passed to `from_pretrained`.
        device: CUDA device index as a string, e.g. "0".
        dtype_fixups: optional {attribute_path: torch.dtype}, applied after
            `from_pretrained`, for submodules the top-level `torch_dtype`
            does not reliably reach. Dotted paths are supported
            (e.g. {"vae": torch.float32, "transformer": torch.float16}).

    Returns the loaded pipeline, offloaded/sliced/tiled and ready to call.
    """
    import torch
    import diffusers

    cls = getattr(diffusers, pipeline_class)
    has_cuda = torch.cuda.is_available()
    # A T4 is Turing (sm_75) and has no bfloat16 support, so fp16 is used even
    # though most upstream examples for these models show bf16.
    dtype = torch.float16 if has_cuda else torch.float32

    log.info("loading %s from %s (dtype=%s)...", pipeline_class, model_id, dtype)
    pipeline = cls.from_pretrained(model_id, torch_dtype=dtype)

    for attr_path, cast_dtype in (dtype_fixups or {}).items():
        obj = pipeline
        *parents, leaf = attr_path.split(".")
        for part in parents:
            obj = getattr(obj, part)
        setattr(obj, leaf, getattr(obj, leaf).to(cast_dtype))

    if has_cuda:
        pipeline.enable_model_cpu_offload(device=f"cuda:{device}")
    else:
        pipeline = pipeline.to("cpu")

    # Attention slicing and VAE tiling/slicing trade a little speed for a lot
    # of headroom - the difference between fitting a T4 and an
    # OutOfMemoryError loading the model at all.
    if hasattr(pipeline, "enable_attention_slicing"):
        pipeline.enable_attention_slicing()
    vae = getattr(pipeline, "vae", None)
    if vae is not None:
        if hasattr(vae, "enable_tiling"):
            vae.enable_tiling()
        if hasattr(vae, "enable_slicing"):
            vae.enable_slicing()

    log.info("%s ready", pipeline_class)
    return pipeline
