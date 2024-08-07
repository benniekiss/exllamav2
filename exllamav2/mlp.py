from __future__ import annotations
import torch
import torch.nn.functional as F
from torch import nn
from exllamav2.module import ExLlamaV2Module
from exllamav2.rmsnorm import ExLlamaV2RMSNorm
from exllamav2.layernorm import ExLlamaV2LayerNorm
from exllamav2.linear import ExLlamaV2Linear
from exllamav2.ext import exllamav2_ext as ext_c, none_tensor
from exllamav2.lora import ExLlamaV2Lora
# from line_profiler import profile

from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from exllamav2.model import ExLlamaV2


class ExLlamaV2MLP(ExLlamaV2Module):

    name: str = "MLP"

    layer_idx: int
    pre_layernorm: ExLlamaV2RMSNorm | ExLlamaV2LayerNorm | None
    post_layernorm: ExLlamaV2RMSNorm | ExLlamaV2LayerNorm | None
    gate_proj: ExLlamaV2Linear | None
    up_proj: ExLlamaV2Linear | None
    down_proj: ExLlamaV2Linear | None

    q_handle: int | None

    temp_lora_size: int

    has_norm: bool
    has_residual: bool

    def __init__(self,
                 model: ExLlamaV2,
                 key: str,
                 layer_idx: int,
                 has_norm: bool = True,
                 has_residual: bool = True):

        super().__init__(model, key)
        cfg = self.model.config

        self.layer_idx = layer_idx
        self.has_norm = has_norm
        self.has_residual = has_residual

        self.q_handle = None
        self.temp_lora_size = 0

        f_a = 0
        f_b = cfg.intermediate_size
        f_c = f_b + cfg.intermediate_size
        f_key = (key + ".mlp." + cfg.arch.fused_mlp_key_12) if cfg.arch.fused_mlp_key_12 else None

        if self.has_norm:
            if cfg.arch.norm == "layernorm":
                self.pre_layernorm = ExLlamaV2LayerNorm(model, key + cfg.arch.norm_key_2)
                self.post_layernorm = ExLlamaV2LayerNorm(model, key + cfg.arch.norm_key_2_post) if cfg.arch.norm_key_2_post else None
            elif cfg.arch.norm == "rmsnorm":
                self.pre_layernorm = ExLlamaV2RMSNorm(model, key + cfg.arch.norm_key_2)
                self.post_layernorm = ExLlamaV2RMSNorm(model, key + cfg.arch.norm_key_2_post) if cfg.arch.norm_key_2_post else None
        else:
            self.pre_layernorm = None
            self.post_layernorm = None

        self.up_proj = ExLlamaV2Linear(model, key + cfg.arch.mlp_key_up, cfg.hidden_size, cfg.intermediate_size, self.model.config.arch.mlp_bias, f_key = f_key, f_beg = f_b, f_end = f_c)
        self.down_proj = ExLlamaV2Linear(model, key + cfg.arch.mlp_key_down, cfg.intermediate_size, cfg.hidden_size, self.model.config.arch.mlp_bias, prescale = cfg.scale_depth)

        self.submodules = [self.up_proj,
                           self.down_proj]
        if self.pre_layernorm:
            self.submodules += [self.pre_layernorm]
        if self.post_layernorm:
            self.submodules += [self.post_layernorm]

        if cfg.arch.mlp_gate:
            self.gate_proj = ExLlamaV2Linear(model, key + cfg.arch.mlp_key_gate, cfg.hidden_size, cfg.intermediate_size, self.model.config.arch.mlp_bias, f_key = f_key, f_beg = f_a, f_end = f_b)
            self.submodules += [self.gate_proj]
        else:
            self.gate_proj = None


    def numel(self) -> int:

        numel = self.up_proj.numel() + \
                self.down_proj.numel()

        if self.model.config.arch.mlp_gate:
            numel += self.gate_proj.numel()

        if self.pre_layernorm is not None:
            numel += self.pre_layernorm.numel()
        if self.post_layernorm is not None:
            numel += self.pre_layernorm.numel()

        return numel


    @torch.inference_mode
    def load(self):

        cfg = self.model.config

        if self.pre_layernorm is not None:
            self.pre_layernorm.load()
        if self.post_layernorm is not None:
            self.post_layernorm.load()

        if cfg.checkpoint_fused_mlp:
            w12 = self.load_weight(self.key + cfg.arch.fused_mlp_key_12)
            w1 = nn.Parameter(w12[:cfg.intermediate_size, :].contiguous())
            w2 = nn.Parameter(w12[cfg.intermediate_size:, :].contiguous())
            w3 = self.load_weight(self.key + cfg.arch.fused_mlp_key_3)
            self.gate_proj.load(w1)
            self.up_proj.load(w2)
            self.down_proj.load(w3)
        else:
            if self.gate_proj is not None: self.gate_proj.load()
            self.up_proj.load()
            self.down_proj.load()

        if self.up_proj.is_quant():
            assert self.gate_proj is None or self.gate_proj.is_quant()
            assert self.up_proj.is_quant(), "Partially quantized MLP layer"
            device_tensors = self.model.get_device_tensors(self.device_idx)
            device_tensors.begin_scratch_alloc()

            if self.has_norm:
                norm_weight = self.pre_layernorm.weight if self.pre_layernorm.weight is not None else none_tensor
                norm_bias = self.pre_layernorm.bias if self.pre_layernorm.bias is not None else none_tensor
                is_rms = isinstance(self.pre_layernorm, ExLlamaV2RMSNorm)
                eps = self.pre_layernorm.variance_epsilon
            else:
                norm_weight = none_tensor
                norm_bias = none_tensor
                is_rms = False
                eps = 0

            if self.post_layernorm is not None:
                post_norm_weight = self.post_layernorm.weight if self.post_layernorm.weight is not None else none_tensor
                post_norm_bias = self.post_layernorm.bias if self.post_layernorm.bias is not None else none_tensor
            else:
                post_norm_weight = none_tensor
                post_norm_bias = none_tensor

            self.q_handle = ext_c.make_q_mlp(norm_weight,
                                             norm_bias,
                                             is_rms,
                                             eps,
                                             0 if self.gate_proj is None else self.gate_proj.q_handle,
                                             self.up_proj.q_handle,
                                             self.down_proj.q_handle,
                                             device_tensors.get_scratch_slice(self.temp_state_size()),
                                             device_tensors.get_scratch_slice(self.temp_a_size()),
                                             device_tensors.get_scratch_slice(self.temp_b_size()),
                                             device_tensors.get_scratch_slice(self.temp_dq_size()),
                                             cfg.max_input_len * cfg.max_batch_size,
                                             cfg.arch.mlp_act_func == "gelu",
                                             self.has_residual,
                                             post_norm_weight,
                                             post_norm_bias,
                                             cfg.arch.residual_stream_fp32)


    def unload(self):

        if self.q_handle is not None:
            ext_c.free_q_mlp(self.q_handle)
            self.q_handle = None

        if self.pre_layernorm is not None: self.pre_layernorm.unload()
        if self.post_layernorm is not None: self.post_layernorm.unload()
        if self.gate_proj is not None: self.gate_proj.unload()
        self.up_proj.unload()
        self.down_proj.unload()


    def weight_footprint(self) -> int:

        if self.model.config.checkpoint_fused_mlp:
            fp = 3 * self.model.config.intermediate_size * self.model.config.hidden_size * 2
        else:
            fp = self.up_proj.weight_footprint() + \
                 self.down_proj.weight_footprint()
            if self.gate_proj is not None:
                fp += self.gate_proj.weight_footprint()

        if self.pre_layernorm is not None:
            fp += self.pre_layernorm.weight_footprint()
        if self.post_layernorm is not None:
            fp += self.post_layernorm.weight_footprint()

        return fp


    def scratch_space_fixed(self) -> int:

        return self.temp_state_size() + \
               self.temp_a_size() + \
               self.temp_b_size() + \
               self.temp_dq_size()


    def scratch_space(self) -> int:

        cfg = self.model.config
        assert cfg.intermediate_size >= cfg.hidden_size
        return self.temp_state_size() + \
               self.temp_a_size() + \
               self.temp_b_size() + \
               self.temp_dq_size()


    def temp_state_size(self) -> int:

        cfg = self.model.config
        return cfg.max_input_len * cfg.max_batch_size * cfg.hidden_size * 2 + 128


    def temp_a_size(self) -> int:

        cfg = self.model.config
        return cfg.max_input_len * cfg.max_batch_size * cfg.intermediate_size * 2 + 128


    def temp_b_size(self) -> int:

        cfg = self.model.config
        return cfg.max_input_len * cfg.max_batch_size * cfg.intermediate_size * 2 + 128


    def temp_dq_size(self) -> int:

        return max(0 if self.gate_proj is None else self.gate_proj.temp_dq_size(),
                   self.up_proj.temp_dq_size(),
                   self.down_proj.temp_dq_size())


    def set_device_idx(self, idx: int):
        super().set_device_idx(idx)

        if self.pre_layernorm is not None:
            self.pre_layernorm.set_device_idx(idx)
        if self.post_layernorm is not None:
            self.post_layernorm.set_device_idx(idx)
        if self.gate_proj is not None: self.gate_proj.set_device_idx(idx)
        self.up_proj.set_device_idx(idx)
        self.down_proj.set_device_idx(idx)


    # @profile
    def forward(self,
                hidden_states: torch.Tensor,
                cache = None,
                attn_params = None,
                past_len = None,
                intermediates: bool = False,
                loras: list[ExLlamaV2Lora] | None = None,
                **kwargs) -> torch.Tensor | dict[str: torch.Tensor]:

        cfg = self.model.config

        if self.q_handle is None or intermediates:
            return self.forward_torch(hidden_states, cache, attn_params, past_len, intermediates, loras = loras, **kwargs)

        if loras is None or self.temp_lora_size == 0:
            pass_loras = []
            pass_lora_temp = none_tensor
        else:
            pass_loras = [id(x) for x in loras]
            pass_lora_temp = torch.empty((self.temp_lora_size,), dtype = torch.half, device = hidden_states.device)

        ext_c.q_mlp_forward_(self.q_handle,
                             hidden_states,
                             pass_loras,
                             pass_lora_temp)

        if cfg.arch.clamp_hidden_states:
            hidden_states.clamp_(-65504, 65504)

        return hidden_states


    def forward_torch(self,
                      hidden_states: torch.Tensor,
                      cache = None,
                      attn_params = None,
                      past_len = None,
                      intermediates: bool = False,
                      loras: list[ExLlamaV2Lora] | None = None,
                      **kwargs) -> torch.Tensor | dict[str: torch.Tensor]:

        cfg = self.model.config

        residual = hidden_states
        post_norm = self.pre_layernorm.forward(hidden_states) \
            if self.pre_layernorm else hidden_states

        if self.gate_proj is not None:
            gate = self.gate_proj.forward(post_norm, loras = loras)
            if cfg.arch.mlp_act_func == "silu":
                y = F.silu(gate)
            elif cfg.arch.mlp_act_func == "gelu":
                y = F.gelu(gate, approximate = "tanh")
            up = self.up_proj.forward(post_norm, loras = loras)
            y *= up
            y.clamp_(min = -65504.0, max = 65504.0)
        else:
            up = self.up_proj.forward(post_norm, loras = loras)
            if cfg.arch.mlp_act_func == "silu":
                y = F.silu(up)
            elif cfg.arch.mlp_act_func == "gelu":
                y = F.gelu(up, approximate = "tanh")

        down = self.down_proj.forward(y, loras = loras)
        if self.post_layernorm:
            down = self.post_layernorm.forward(down, output_fp32 = cfg.arch.residual_stream_fp32)
        hidden_states = down + residual if self.has_residual else down

        if cfg.arch.residual_stream_fp32:
            hidden_states = hidden_states.float()
        elif cfg.arch.clamp_hidden_states:
            hidden_states = hidden_states.clamp(-65504, 65504)

        if intermediates:
            return {"post_norm": post_norm,
                    "pre_down": y,
                    "hidden_states": hidden_states}
        else:
            return hidden_states


    def update_loras(self):

        if self.q_handle is None: return

        if self.gate_proj is None:
            gate_proj_lora_a = {}
            gate_proj_lora_b = {}
        else:
            gate_proj_lora_a = { id(k): v for k, v in self.gate_proj.lora_a_tensors.items() }
            gate_proj_lora_b = { id(k): v for k, v in self.gate_proj.lora_b_tensors.items() }

        up_proj_lora_a = { id(k): v for k, v in self.up_proj.lora_a_tensors.items() }
        up_proj_lora_b = { id(k): v for k, v in self.up_proj.lora_b_tensors.items() }
        down_proj_lora_a = { id(k): v for k, v in self.down_proj.lora_a_tensors.items() }
        down_proj_lora_b = { id(k): v for k, v in self.down_proj.lora_b_tensors.items() }

        temp_lora_size = ext_c.q_mlp_set_loras(self.q_handle,
                                               gate_proj_lora_a,
                                               gate_proj_lora_b,
                                               up_proj_lora_a,
                                               up_proj_lora_b,
                                               down_proj_lora_a,
                                               down_proj_lora_b)

        self.temp_lora_size = temp_lora_size * self.model.config.max_batch_size * self.model.config.max_input_len


    def is_quant(self):
        return self.q_handle is not None


    def rank_reduce(self, k):

        if self.gate_proj is not None: self.gate_proj.rank_reduce(k)
        self.up_proj.rank_reduce(k)
        self.down_proj.rank_reduce(k)
