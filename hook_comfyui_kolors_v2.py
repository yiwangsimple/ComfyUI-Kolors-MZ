import os
from types import MethodType
import warnings
from comfy.model_detection import *
import comfy.model_detection as model_detection
import comfy.supported_models
import comfy.utils

import torch
from torch import nn
from comfy import model_base
from comfy.model_base import sdxl_pooled, CLIPEmbeddingNoiseAugmentation, Timestep, ModelType

from comfy.ldm.modules.diffusionmodules.openaimodel import UNetModel

class KolorsUNetModel(UNetModel):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.encoder_hid_proj = nn.Linear(4096, 2048, bias=True)

    def forward(self, *args, **kwargs):
        with torch.cuda.amp.autocast(enabled=False):
            if "context" in kwargs:
                context = kwargs["context"].to(torch.float32)
                kwargs["context"] = self.encoder_hid_proj(context)
                kwargs["context"] = kwargs["context"].to(context.dtype)

            result = super().forward(*args, **kwargs)
            return result

class KolorsSDXL(model_base.SDXL):
    def __init__(self, model_config, model_type=ModelType.EPS, device=None):
        model_base.BaseModel.__init__(
            self, model_config, model_type, device=device, unet_model=KolorsUNetModel)
        self.embedder = Timestep(256)
        self.noise_augmentor = CLIPEmbeddingNoiseAugmentation(
            **{"noise_schedule_config": {"timesteps": 1000, "beta_schedule": "squaredcos_cap_v2"}, "timestep_dim": 1280})

    def encode_adm(self, **kwargs):
        clip_pooled = sdxl_pooled(kwargs, self.noise_augmentor)
        width = kwargs.get("width", 768)
        height = kwargs.get("height", 768)
        crop_w = kwargs.get("crop_w", 0)
        crop_h = kwargs.get("crop_h", 0)
        target_width = kwargs.get("target_width", width)
        target_height = kwargs.get("target_height", height)

        out = []
        device = clip_pooled.device
        dtype = clip_pooled.dtype

        out.append(self.embedder(torch.tensor([height], device=device, dtype=dtype)))
        out.append(self.embedder(torch.tensor([width], device=device, dtype=dtype)))
        out.append(self.embedder(torch.tensor([crop_h], device=device, dtype=dtype)))
        out.append(self.embedder(torch.tensor([crop_w], device=device, dtype=dtype)))
        out.append(self.embedder(torch.tensor([target_height], device=device, dtype=dtype)))
        out.append(self.embedder(torch.tensor([target_width], device=device, dtype=dtype)))

        flat = torch.cat(out).flatten().unsqueeze(0).repeat(clip_pooled.shape[0], 1)
        return torch.cat((clip_pooled, flat), dim=1)

class KolorsSupported(comfy.supported_models.SDXL):
    unet_config = {
        "model_channels": 320,
        "use_linear_in_transformer": True,
        "transformer_depth": [0, 0, 2, 2, 10, 10],
        "context_dim": 2048,
        "adm_in_channels": 5632,
        "use_temporal_attention": False,
    }

    def get_model(self, state_dict, prefix="", device=None):
        out = KolorsSDXL(self, model_type=self.model_type(
            state_dict, prefix), device=device,)
        out.__class__ = model_base.SDXL
        if self.inpaint_model():
            out.set_inpaint()
        return out

def kolors_unet_config_from_diffusers_unet(state_dict, dtype=None):
    match = {}
    transformer_depth = []

    attn_res = 1
    down_blocks = count_blocks(state_dict, "down_blocks.{}")
    for i in range(down_blocks):
        attn_blocks = count_blocks(
            state_dict, "down_blocks.{}.attentions.".format(i) + '{}')
        res_blocks = count_blocks(
            state_dict, "down_blocks.{}.resnets.".format(i) + '{}')
        for ab in range(attn_blocks):
            transformer_count = count_blocks(
                state_dict, "down_blocks.{}.attentions.{}.transformer_blocks.".format(i, ab) + '{}')
            transformer_depth.append(transformer_count)
            if transformer_count > 0:
                match["context_dim"] = state_dict["down_blocks.{}.attentions.{}.transformer_blocks.0.attn2.to_k.weight".format(
                    i, ab)].shape[1]

        attn_res *= 2
        if attn_blocks == 0:
            for i in range(res_blocks):
                transformer_depth.append(0)

    match["transformer_depth"] = transformer_depth

    match["model_channels"] = state_dict["conv_in.weight"].shape[0]
    match["in_channels"] = state_dict["conv_in.weight"].shape[1]
    match["adm_in_channels"] = None
    if "class_embedding.linear_1.weight" in state_dict:
        match["adm_in_channels"] = state_dict["class_embedding.linear_1.weight"].shape[1]
    elif "add_embedding.linear_1.weight" in state_dict:
        match["adm_in_channels"] = state_dict["add_embedding.linear_1.weight"].shape[1]

    Kolors = {'use_checkpoint': False, 'image_size': 32, 'out_channels': 4, 'use_spatial_transformer': True, 'legacy': False,
              'num_classes': 'sequential', 'adm_in_channels': 5632, 'dtype': dtype, 'in_channels': 4, 'model_channels': 320,
              'num_res_blocks': [2, 2, 2], 'transformer_depth': [0, 0, 2, 2, 10, 10], 'channel_mult': [1, 2, 4], 'transformer_depth_middle': 10,
              'use_linear_in_transformer': True, 'context_dim': 2048, 'num_head_channels': 64, 'transformer_depth_output': [0, 0, 0, 2, 2, 2, 10, 10, 10],
              'use_temporal_attention': False, 'use_temporal_resblock': False}

    supported_models = [Kolors]

    for unet_config in supported_models:
        matches = True
        for k in match:
            if match[k] != unet_config[k]:
                print("key {} does not match".format(
                    k), match[k], "||", unet_config[k])
                matches = False
                break
        if matches:
            return convert_config(unet_config)
    return None

def load_clipvision_336_from_sd(sd, prefix="", convert_keys=False):
    from comfy.clip_vision import ClipVisionModel, convert_to_transformers

    json_config = os.path.join(os.path.dirname(
        os.path.realpath(__file__)), "clip_vit_336", "config.json")

    clip = ClipVisionModel(json_config)

    m, u = clip.load_sd(sd)
    if len(m) > 0:
        logging.warning("missing clip vision: {}".format(m))
    u = set(u)
    keys = list(sd.keys())
    for k in keys:
        if k not in u:
            t = sd.pop(k)
            del t

    return clip

class apply_kolors:
    def __enter__(self):
        import comfy.ldm.modules.diffusionmodules.openaimodel
        import comfy.utils
        import comfy.clip_vision

        self.original_load_clipvision_from_sd = comfy.clip_vision.load_clipvision_from_sd
        comfy.clip_vision.load_clipvision_from_sd = load_clipvision_336_from_sd

        self.original_UNET_MAP_BASIC = comfy.utils.UNET_MAP_BASIC.copy()
        comfy.utils.UNET_MAP_BASIC.add(
            ("encoder_hid_proj.weight", "encoder_hid_proj.weight"),
        )
        comfy.utils.UNET_MAP_BASIC.add(
            ("encoder_hid_proj.bias", "encoder_hid_proj.bias"),
        )

        self.original_unet_config_from_diffusers_unet = model_detection.unet_config_from_diffusers_unet
        model_detection.unet_config_from_diffusers_unet = kolors_unet_config_from_diffusers_unet

        import comfy.supported_models
        self.original_supported_models = comfy.supported_models.models
        comfy.supported_models.models = [KolorsSupported]

    def __exit__(self, type, value, traceback):
        import comfy.ldm.modules.diffusionmodules.openaimodel
        import comfy.utils
        comfy.utils.UNET_MAP_BASIC = self.original_UNET_MAP_BASIC

        model_detection.unet_config_from_diffusers_unet = self.original_unet_config_from_diffusers_unet

        import comfy.supported_models
        comfy.supported_models.models = self.original_supported_models

        import comfy.clip_vision
        comfy.clip_vision.load_clipvision_from_sd = self.original_load_clipvision_from_sd