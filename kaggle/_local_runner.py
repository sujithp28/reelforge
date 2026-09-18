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
    device_map: "str | dict[str, int] | None" = None,
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
            Not compatible with `device_map` (accelerate hooks a dispatched
            module's `.to()`, so a manual cast after load would corrupt it).
        device_map: "balanced" or an explicit {component_name: device_index}
            dict (e.g. {"transformer": 1}), to have accelerate stream each
            shard straight from its mmap'd safetensors file to its target
            GPU during `from_pretrained`, instead of fully materialising the
            checkpoint in host RAM first. For a checkpoint with no fp16
            variant (fp32-only on disk), this is the difference between
            fitting a low-RAM session and OOM-ing partway through loading,
            since converting fp32->fp16 for the whole model in host RAM is
            what `low_cpu_mem_usage=True` alone still doesn't avoid. Spreads
            across both T4s rather than offloading to CPU, so it needs the
            checkpoint's fp16 footprint to fit in combined GPU VRAM instead
            of in system RAM. Prefer an explicit dict over "balanced" when a
            single component is large enough that "balanced" would split it
            layer-by-layer across devices - that produces tensors on two
            devices within one forward pass, which crashes at inference
            time, not at load time.

    Returns the loaded pipeline, offloaded/sliced/tiled and ready to call.
    """
    import importlib

    import torch
    import diffusers

    if device_map and dtype_fixups:
        raise ValueError("device_map and dtype_fixups cannot be combined")

    cls = getattr(diffusers, pipeline_class)
    has_cuda = torch.cuda.is_available()
    # A T4 is Turing (sm_75) and has no bfloat16 support, so fp16 is used even
    # though most upstream examples for these models show bf16.
    dtype = torch.float16 if has_cuda else torch.float32

    log.info("loading %s from %s (dtype=%s, device_map=%s)...",
              pipeline_class, model_id, dtype, device_map)
    # low_cpu_mem_usage=True is explicit rather than relying on diffusers'
    # default: for an fp32-only checkpoint (no fp16 variant to download
    # instead), this is what keeps shard loading from materialising the full
    # fp32 state dict in host RAM before casting. It's not always enough on
    # its own (a big enough model still peaks over a low-RAM session's
    # ceiling), which is what device_map is for.
    if isinstance(device_map, dict) and has_cuda:
        # DiffusionPipeline.from_pretrained() only accepts device_map as a
        # string ("balanced" etc) - it has no notion of "pin this named
        # component to this device". To get that, load each named component
        # with its own model class and single-device placement, then hand
        # the assembled objects to from_pretrained(), which uses whatever
        # components it's given and only loads the rest (tokenizer,
        # scheduler) itself.
        config = cls.load_config(model_id)
        components = {}
        for name, target_device in device_map.items():
            library_name, class_name = config[name]
            component_cls = getattr(importlib.import_module(library_name), class_name)
            components[name] = component_cls.from_pretrained(
                model_id, subfolder=name, torch_dtype=dtype,
                low_cpu_mem_usage=True, device_map=f"cuda:{target_device}",
            )
        pipeline = cls.from_pretrained(
            model_id, torch_dtype=dtype, low_cpu_mem_usage=True, **components,
        )
    else:
        kwargs = {"torch_dtype": dtype, "low_cpu_mem_usage": True}
        if device_map and has_cuda:
            kwargs["device_map"] = device_map
        pipeline = cls.from_pretrained(model_id, **kwargs)

    for attr_path, cast_dtype in (dtype_fixups or {}).items():
        obj = pipeline
        *parents, leaf = attr_path.split(".")
        for part in parents:
            obj = getattr(obj, part)
        setattr(obj, leaf, getattr(obj, leaf).to(cast_dtype))

    if device_map and has_cuda:
        pass  # already placed on its target device(s) by accelerate
    elif has_cuda:
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
