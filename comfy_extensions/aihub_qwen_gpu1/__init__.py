"""Wallpaper-only Qwen encoder placement; install separately after operator review.

The runner must own BOTH physical GPUs before this workflow executes. Keep
cuda:0 as ComfyUI's default, and expose both GPUs in the UUID order below.
This node deliberately does not change process-wide device policy or sampling.
"""
import logging
from uuid import UUID

import torch
import comfy.model_management as mm
import comfy.sd
import folder_paths


def _uuid(value):
    return UUID(str(value).lower().removeprefix("gpu-"))


def _check_placement(gpu0_uuid, gpu1_uuid):
    if (not torch.cuda.is_available() or torch.cuda.device_count() != 2 or
            torch.cuda.current_device() != 0 or mm.intermediate_device().type != "cpu"):
        raise RuntimeError("qwen_gpu1_requires_two_gpus_default_zero_cpu_intermediates")
    expected = (_uuid(gpu0_uuid), _uuid(gpu1_uuid))
    if expected[0] == expected[1] or any(
            _uuid(getattr(torch.cuda.get_device_properties(i), "uuid", "")) != expected[i]
            for i in (0, 1)):
        raise RuntimeError("qwen_gpu1_physical_device_mapping_mismatch")


def _check_cpu(value):
    if torch.is_tensor(value):
        if value.device.type != "cpu":
            raise RuntimeError("qwen_conditioning_not_on_cpu_before_gpu1_release")
    elif isinstance(value, dict):
        for item in value.values():
            _check_cpu(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_cpu(item)


class _GPU1CLIP:
    """Keep the integrated sampler's CLIP interface, with bounded GPU1 residency."""
    def __init__(self, clip, gpu0_uuid, gpu1_uuid):
        self._clip, self._uuids = clip, (gpu0_uuid, gpu1_uuid)

    def __getattr__(self, name):
        return getattr(self._clip, name)

    def clone(self, *args, **kwargs):
        return _GPU1CLIP(self._clip.clone(*args, **kwargs), *self._uuids)

    def _encode(self, method, *args, **kwargs):
        _check_placement(*self._uuids)
        if (self._clip.patcher.load_device != torch.device("cuda:1") or
                self._clip.patcher.offload_device != torch.device("cpu")):
            raise RuntimeError("qwen_encoder_device_contract_changed")
        # ComfyUI 0.15.1 clears only the CURRENT CUDA device. Offload the
        # encoder and clear GPU1 on success AND failure, then restore GPU0.
        with torch.cuda.device(1):
            try:
                result = getattr(self._clip, method)(*args, **kwargs)
                _check_cpu(result)
                logging.info("AI-HUB Qwen conditioning completed on GPU1; returning CPU conditioning")
                return result
            finally:
                mm.free_memory(1e30, self._clip.patcher.load_device)
                mm.soft_empty_cache(force=True)

    def encode_from_tokens_scheduled(self, *args, **kwargs):
        return self._encode("encode_from_tokens_scheduled", *args, **kwargs)

    def encode_from_tokens(self, *args, **kwargs):
        return self._encode("encode_from_tokens", *args, **kwargs)


class AIHubQwenGPU1CLIPLoader:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {
            "clip_name": (["qwen_2.5_vl_7b_fp8_scaled.safetensors"],),
            "gpu0_uuid": ("STRING", {"default": ""}),
            "gpu1_uuid": ("STRING", {"default": ""}),
        }}

    RETURN_TYPES = ("CLIP",)
    FUNCTION = "load_clip"
    CATEGORY = "AI-HUB/wallpaper"

    def load_clip(self, clip_name, gpu0_uuid, gpu1_uuid):
        _check_placement(gpu0_uuid, gpu1_uuid)
        if clip_name != "qwen_2.5_vl_7b_fp8_scaled.safetensors":
            raise ValueError("qwen_gpu1_encoder_not_allowlisted")
        # Build the model on CPU, so no GPU1 model can remain loaded if the
        # workflow fails before calling the sampler. Encoding loads onto GPU1.
        clip = comfy.sd.load_clip(
            ckpt_paths=[folder_paths.get_full_path_or_raise("text_encoders", clip_name)],
            embedding_directory=folder_paths.get_folder_paths("embeddings"),
            clip_type=comfy.sd.CLIPType.QWEN_IMAGE,
            model_options={"load_device": torch.device("cuda:1"),
                           "offload_device": torch.device("cpu"),
                           "initial_device": torch.device("cpu")})
        return (_GPU1CLIP(clip, gpu0_uuid, gpu1_uuid),)


NODE_CLASS_MAPPINGS = {"AIHubQwenGPU1CLIPLoader": AIHubQwenGPU1CLIPLoader}
