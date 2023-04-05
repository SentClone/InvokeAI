import re
from pathlib import Path
from typing import Optional

import torch
from compel import Compel
from diffusers.models import UNet2DConditionModel
from safetensors.torch import load_file
from torch.utils.hooks import RemovableHandle
from transformers import CLIPTextModel

from ldm.invoke.devices import choose_torch_device

"""
This module supports loading LoRA weights trained with https://github.com/kohya-ss/sd-scripts
To be removed once support for diffusers LoRA weights is well supported
"""


class LoRALayer:
    lora_name: str
    name: str
    scale: float
    up: torch.nn.Module
    mid: Optional[torch.nn.Module] = None
    down: torch.nn.Module

    def __init__(self, lora_name: str, name: str, rank=4, alpha=1.0):
        self.lora_name = lora_name
        self.name = name
        self.scale = alpha / rank if (alpha and rank) else 1.0

    def forward(self, lora, input_h, output):
        if self.mid is None:
            output = (
                output
                + self.up(self.down(*input_h)) * lora.multiplier * self.scale
            )
        else:
            output = (
                output
                + self.up(self.mid(self.down(*input_h))) * lora.multiplier * self.scale
            )
        return output

class LoHALayer:
    lora_name: str
    name: str
    scale: float

    w1_a: torch.Tensor
    w1_b: torch.Tensor
    w2_a: torch.Tensor
    w2_b: torch.Tensor
    t1: Optional[torch.Tensor] = None
    t2: Optional[torch.Tensor] = None

    org_module: torch.nn.Module

    def __init__(self, lora_name: str, name: str, rank=4, alpha=1.0):
        self.lora_name = lora_name
        self.name = name
        self.scale = alpha / rank if (alpha and rank) else 1.0

    def forward(self, lora, input_h, output):

        # implementation according to lycoris
        # i'm not so sure what happens here, but to properly work
        # i moved scaling from weight calculation to output calculation
        # https://github.com/KohakuBlueleaf/LyCORIS/blob/main/lycoris/loha.py#L175
        if self.t1 is None:
            diff_weight = ((self.w1_a @ self.w1_b) * (self.w2_a @ self.w2_b))
            weight = self.org_module.weight.data.reshape(diff_weight.shape) + diff_weight

        else:
            rebuild1 = torch.einsum('i j k l, j r, i p -> p r k l', self.t1, self.w1_b, self.w1_a)
            rebuild2 = torch.einsum('i j k l, j r, i p -> p r k l', self.t2, self.w2_b, self.w2_a)
            weight = self.org_module.weight.data + rebuild1 * rebuild2

        if type(self.org_module) == torch.nn.Conv2d:
            op = torch.nn.functional.conv2d
            extra_args = dict(
                stride=self.org_module.stride,
                padding=self.org_module.padding,
                dilation=self.org_module.dilation,
                groups=self.org_module.groups,
            )

        else:
            op = torch.nn.functional.linear
            extra_args = {}
        
        bias = None if self.org_module.bias is None else self.org_module.bias.data
        return output + op(
            *input_h,
            weight.view(self.org_module.weight.shape),
            bias,
            **extra_args,
        ) * lora.multiplier * self.scale
        

        # implementation according to a1111-sd-webui-locon extension
        # https://github.com/KohakuBlueleaf/a1111-sd-webui-locon/blob/main/scripts/main.py#L248
        def pro3(t, wa, wb):
            temp = torch.einsum('i j k l, j r -> i r k l', t, wb)
            return torch.einsum('i j k l, i r -> r j k l', temp, wa)

        bias = 0 # TODO: implement bias
        if self.t1 is None:
            return output + op(
                *input_h,
                ((self.w1_a @ self.w1_b) * (self.w2_a @ self.w2_b) + bias).view(self.org_module.weight.shape),
                bias=None,
                **extra_args
            ) * lora.multiplier * self.scale

        else:
            return output + op(
                *input_h,
                (pro3(self.t1, self.w1_a, self.w1_b) 
                 * pro3(self.t2, self.w2_a, self.w2_b) + bias).view(self.org_module.weight.shape),
                bias=None,
                **extra_args
            ) * lora.multiplier * self.scale


class LoRAModuleWrapper:
    unet: UNet2DConditionModel
    text_encoder: CLIPTextModel
    hooks: list[RemovableHandle]

    def __init__(self, unet, text_encoder):
        self.unet = unet
        self.text_encoder = text_encoder
        self.hooks = []
        self.text_modules = None
        self.unet_modules = None

        self.applied_loras = {}
        self.loaded_loras = {}

        self.UNET_TARGET_REPLACE_MODULE = ["Transformer2DModel", "Attention", "ResnetBlock2D", "Downsample2D", "Upsample2D", "SpatialTransformer"]
        self.TEXT_ENCODER_TARGET_REPLACE_MODULE = ["ResidualAttentionBlock", "CLIPAttention", "CLIPMLP"]
        self.LORA_PREFIX_UNET = "lora_unet"
        self.LORA_PREFIX_TEXT_ENCODER = "lora_te"


        def find_modules(
            prefix, root_module: torch.nn.Module, target_replace_modules
        ) -> dict[str, torch.nn.Module]:
            mapping = {}
            for name, module in root_module.named_modules():
                if module.__class__.__name__ in target_replace_modules:
                    for child_name, child_module in module.named_modules():
                        layer_type = child_module.__class__.__name__
                        if layer_type == "Linear" or (
                            layer_type == "Conv2d"
                            and child_module.kernel_size in [(1, 1), (3, 3)]
                        ):
                            lora_name = prefix + "." + name + "." + child_name
                            lora_name = lora_name.replace(".", "_")
                            mapping[lora_name] = child_module
                            self.apply_module_forward(child_module, lora_name)
            return mapping

        if self.text_modules is None:
            self.text_modules = find_modules(
                self.LORA_PREFIX_TEXT_ENCODER,
                text_encoder,
                self.TEXT_ENCODER_TARGET_REPLACE_MODULE,
            )

        if self.unet_modules is None:
            self.unet_modules = find_modules(
                self.LORA_PREFIX_UNET, unet, self.UNET_TARGET_REPLACE_MODULE
            )


    def lora_forward_hook(self, name):
        wrapper = self

        def lora_forward(module, input_h, output):
            if len(wrapper.loaded_loras) == 0:
                return output

            for lora in wrapper.applied_loras.values():
                layer = lora.layers.get(name, None)
                if layer is None:
                    continue
                output = layer.forward(lora, input_h, output)
            return output

        return lora_forward

    def apply_module_forward(self, module, name):
        handle = module.register_forward_hook(self.lora_forward_hook(name))
        self.hooks.append(handle)

    def clear_hooks(self):
        for hook in self.hooks:
            hook.remove()

        self.hooks.clear()

    def clear_applied_loras(self):
        self.applied_loras.clear()

    def clear_loaded_loras(self):
        self.loaded_loras.clear()

class LoRA:
    name: str
    layers: dict[str, LoRALayer]
    device: torch.device
    dtype: torch.dtype
    wrapper: LoRAModuleWrapper
    multiplier: float

    def __init__(self, name: str, device, dtype, wrapper, multiplier=1.0):
        self.name = name
        self.layers = {}
        self.multiplier = multiplier
        self.device = device
        self.dtype = dtype
        self.wrapper = wrapper
        self.rank = None
        self.alpha = None

    def load_from_dict(self, state_dict):
        state_dict_groupped = dict()
        is_loha = False

        for key, value in state_dict.items():
            stem, leaf = key.split(".", 1)
            if stem not in state_dict_groupped:
                state_dict_groupped[stem] = dict()
            state_dict_groupped[stem][leaf] = value

            if leaf.endswith("alpha"):
                if self.alpha is None:
                    self.alpha = value.item()
                continue

            if (
                stem.startswith(self.wrapper.LORA_PREFIX_TEXT_ENCODER)
                or stem.startswith(self.wrapper.LORA_PREFIX_UNET)
            ):
                if (
                    self.rank is None
                    and leaf == "lora_down.weight"
                    and len(value.size()) == 2
                ):
                    self.rank = value.shape[0]

            if "hada_t1" in leaf:
                is_loha = True


        for stem, values in state_dict_groupped.items():
            if stem.startswith(self.wrapper.LORA_PREFIX_TEXT_ENCODER):
                wrapped = self.wrapper.text_modules.get(stem, None)
            elif stem.startswith(self.wrapper.LORA_PREFIX_UNET):
                wrapped = self.wrapper.unet_modules.get(stem, None)
            else:
                continue

            if wrapped is None:
                print(f">> Missing layer: {stem}")
                continue

            print(f"{stem}")
            print(f"{list(values.keys())}")

            # lora and locon
            if "lora_down.weight" in values:
                value_down = values["lora_down.weight"]
                value_mid  = values.get("lora_mid.weight", None)
                value_up   = values["lora_up.weight"]

                if type(wrapped) == torch.nn.Conv2d:
                    if value_mid is not None:
                        layer_down = torch.nn.Conv2d(value_down.shape[1], value_down.shape[0], (1, 1), bias=False)
                        layer_mid  = torch.nn.Conv2d(value_mid.shape[1], value_mid.shape[0], wrapped.kernel_size, wrapped.stride, wrapped.padding, bias=False)
                    else:
                        layer_down = torch.nn.Conv2d(value_down.shape[1], value_down.shape[0], wrapped.kernel_size, wrapped.stride, wrapped.padding, bias=False)
                        layer_mid  = None

                    layer_up = torch.nn.Conv2d(value_up.shape[1], value_up.shape[0], (1, 1), bias=False)

                elif type(wrapped) == torch.nn.Linear:
                    layer_down = torch.nn.Linear(value_down.shape[1], value_down.shape[0], bias=False)
                    layer_mid  = None
                    layer_up   = torch.nn.Linear(value_up.shape[1], value_up.shape[0], bias=False)

                else:
                    print(
                        f">> Encountered unknown lora layer module in {self.name}: {stem} - {type(wrapped).__name__}"
                    )
                    return


                with torch.no_grad():
                    layer_down.weight.copy_(value_down)
                    if layer_mid is not None:
                        layer_mid.weight.copy_(value_mid)
                    layer_up.weight.copy_(value_up)


                layer_down.to(device=self.device, dtype=self.dtype)
                if layer_mid is not None:
                    layer_mid.to(device=self.device, dtype=self.dtype)
                layer_up.to(device=self.device, dtype=self.dtype)


                alpha = None
                if "alpha" in values:
                    alpha = values["alpha"].item()


                layer = LoRALayer(self.name, stem, self.rank, alpha)
                layer.down = layer_down
                layer.mid = layer_mid
                layer.up = layer_up

            # loha
            elif "hada_w1_b" in values:

                alpha = None
                if "alpha" in values:
                    alpha = values["alpha"].item()

                rank = values["hada_w1_b"].shape[0]

                layer = LoHALayer(self.name, stem, rank, alpha)
                layer.org_module = wrapped

                layer.w1_a = values["hada_w1_a"].to(device=self.device, dtype=self.dtype).requires_grad_(False)
                layer.w1_b = values["hada_w1_b"].to(device=self.device, dtype=self.dtype).requires_grad_(False)
                layer.w2_a = values["hada_w2_a"].to(device=self.device, dtype=self.dtype).requires_grad_(False)
                layer.w2_b = values["hada_w2_b"].to(device=self.device, dtype=self.dtype).requires_grad_(False)

                if type(wrapped) == torch.nn.Conv2d and wrapped.kernel_size != (1, 1):
                    layer.t1 = values["hada_t1"].to(device=self.device, dtype=self.dtype).requires_grad_(False)
                    layer.t2 = values["hada_t2"].to(device=self.device, dtype=self.dtype).requires_grad_(False)

                else:
                    layer.t1 = None
                    layer.t2 = None

            else:
                print(
                    f">> Encountered unknown lora layer module in {self.name}: {stem} - {type(wrapped).__name__}"
                )
                return

            self.layers[stem] = layer


class KohyaLoraManager:
    def __init__(self, pipe, lora_path):
        self.unet = pipe.unet
        self.lora_path = lora_path
        self.wrapper = LoRAModuleWrapper(pipe.unet, pipe.text_encoder)
        self.text_encoder = pipe.text_encoder
        self.device = torch.device(choose_torch_device())
        self.dtype = pipe.unet.dtype
        self.loras_to_load = {}

    def load_lora_module(self, name, path_file, multiplier: float = 1.0):
        print(f"   | Found lora {name} at {path_file}")
        if path_file.suffix == ".safetensors":
            checkpoint = load_file(path_file.absolute().as_posix(), device="cpu")
        else:
            checkpoint = torch.load(path_file, map_location="cpu")

        lora = LoRA(name, self.device, self.dtype, self.wrapper, multiplier)
        lora.load_from_dict(checkpoint)
        self.wrapper.loaded_loras[name] = lora

        return lora

    def apply_lora_model(self, name, mult: float = 1.0):
        for suffix in ["ckpt", "safetensors", "pt"]:
            path_file = Path(self.lora_path, f"{name}.{suffix}")
            if path_file.exists():
                print(f"   | Loading lora {path_file.name} with weight {mult}")
                break
        if not path_file.exists():
            print(f"   ** Unable to find lora: {name}")
            return

        lora = self.wrapper.loaded_loras.get(name, None)
        if lora is None:
            lora = self.load_lora_module(name, path_file, mult)

        lora.multiplier = mult
        self.wrapper.applied_loras[name] = lora

    def unload_applied_loras(self, loras_to_load):
        # unload any lora's not defined by loras_to_load
        for name in list(self.wrapper.applied_loras.keys()):
            if name not in loras_to_load:
                self.unload_applied_lora(name)

    def unload_applied_lora(self, lora_name: str):
        if lora_name in self.wrapper.applied_loras:
            del self.wrapper.applied_loras[lora_name]

    def unload_lora(self, lora_name: str):
        if lora_name in self.wrapper.loaded_loras:
            del self.wrapper.loaded_loras[lora_name]

    def clear_loras(self):
        self.loras_to_load = {}
        self.wrapper.clear_applied_loras()
