#
# Copyright 2016 The BigDL Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# This file is adapted from
# https://huggingface.co/THUDM/chatglm2-6b-32k/blob/main/configuration_chatglm.py
#

import torch
from typing import Optional, Tuple, Union
from ipex_llm.transformers.models.utils import update_past_key_value
from ipex_llm.transformers.npu_models.chatglm import repeat_kv
from transformers.modeling_outputs import BaseModelOutputWithPast
import math


def chatglm4_model_forward(
    self,
    input_ids,
    position_ids: Optional[torch.Tensor] = None,
    attention_mask: Optional[torch.BoolTensor] = None,
    full_attention_mask: Optional[torch.BoolTensor] = None,
    past_key_values: Optional[Tuple[Tuple[torch.Tensor, torch.Tensor], ...]]=None,
    inputs_embeds: Optional[torch.Tensor] = None,
    use_cache: Optional[bool] = None,
    output_hidden_states: Optional[bool] = None,
    return_dict: Optional[bool] = None,
) -> Union[Tuple, BaseModelOutputWithPast]:
    output_hidden_states = (
        output_hidden_states if output_hidden_states is not None else
        self.config.output_hidden_states
    )
    use_cache = use_cache if use_cache is not None else self.config.use_cache
    return_dict = return_dict if return_dict is not None else self.config.use_return_dict

    if inputs_embeds is None:
        batch_size, seq_length = input_ids.shape
        inputs_embeds = self.embedding(input_ids)
    else:
        batch_size, seq_length, _ = inputs_embeds.shape
        input_ids = torch.empty((batch_size, seq_length),
                                dtype=inputs_embeds.dtype, device=inputs_embeds.device)

    if full_attention_mask is None:
        if (attention_mask is not None and not attention_mask.all()) or\
                (past_key_values and seq_length != 1):
            full_attention_mask = self.get_masks(input_ids,
                                                 past_key_values,
                                                 padding_mask=attention_mask)

    # Rotary positional embeddings
    rotary_pos_emb = self.rotary_pos_emb(self.seq_length)
    if position_ids is not None:
        rotary_pos_emb = rotary_pos_emb[position_ids]
    else:
        rotary_pos_emb = rotary_pos_emb[None, :seq_length]

    # ipex-llm changes begin:
    # generate `causal_mask` and replace `full_attention_mask` with it

    # `full_attention_mask` is not None only when
    #  `past_key_values` is not None and `seq_length` > 1
    if full_attention_mask is not None:
        causal_mask = torch.zeros([batch_size, 1, seq_length, full_attention_mask.size(-1)],
                                  dtype=inputs_embeds.dtype, device=inputs_embeds.device)
        mask_value = torch.finfo(inputs_embeds.dtype).min
        causal_mask.masked_fill_(full_attention_mask, mask_value)
    else:
        causal_mask = None

    hidden_states, presents, all_hidden_states, all_self_attentions = chatglm4_encoder_forward(
        self.encoder, inputs_embeds, causal_mask, rotary_pos_emb=rotary_pos_emb,
        kv_caches=past_key_values, use_cache=use_cache, output_hidden_states=output_hidden_states
    )
    # ipex-llm changes end

    if presents is not None and type(presents) is torch.Tensor:
        presents = presents.split(1, dim=0)
        presents = list(presents)
        presents = [list(x.squeeze(0).split(1, dim=0)) for x in presents]
        presents = [tuple([x.squeeze(0) for x in y]) for y in presents]
        presents = tuple(presents)

    if not return_dict:
        return tuple(v for v in [hidden_states, presents, all_hidden_states, all_self_attentions]
                     if v is not None)

    return BaseModelOutputWithPast(
        last_hidden_state=hidden_states,
        past_key_values=presents,
        hidden_states=all_hidden_states,
        attentions=all_self_attentions,
    )


def chatglm4_encoder_forward(
    self, hidden_states, attention_mask, rotary_pos_emb, kv_caches=None,
    use_cache: Optional[bool] = True,
    output_hidden_states: Optional[bool] = False,
):
    if not kv_caches:
        kv_caches = [None for _ in range(self.num_layers)]
    presents = () if use_cache else None
    if self.gradient_checkpointing and self.training:
        if use_cache:
            use_cache = False

    all_self_attentions = None
    all_hidden_states = () if output_hidden_states else None
    for index in range(self.num_layers):
        if output_hidden_states:
            all_hidden_states = all_hidden_states + (hidden_states,)

        layer = self._get_layer(index)
        if self.gradient_checkpointing and self.training:
            layer_ret = torch.utils.checkpoint.checkpoint(
                layer,
                hidden_states,
                attention_mask,
                rotary_pos_emb,
                kv_caches[index],
                use_cache,
                use_reentrant=False
            )
        else:
            # if kv_caches[index] is not None:
            layer_ret = layer(
                hidden_states,
                attention_mask,
                rotary_pos_emb,
                kv_cache=kv_caches[index],
                use_cache=use_cache
            )
        hidden_states, kv_cache = layer_ret

        # ipex-llm changes start: use tuple format kv cache
        if use_cache:
            presents = presents + (kv_cache,)
        # ipex-llm changes end

    if output_hidden_states:
        all_hidden_states = all_hidden_states + (hidden_states,)

    # Final layer norm.
    if self.post_layer_norm:
        hidden_states = self.final_layernorm(hidden_states)

    return hidden_states, presents, all_hidden_states, all_self_attentions


@torch.jit.script
def apply_rotary_pos_emb(x: torch.Tensor, rope_cache: torch.Tensor) -> torch.Tensor:
    # x: [b, np, sq, hn]
    b, np, sq, hn = x.size(0), x.size(1), x.size(2), x.size(3)
    rot_dim = rope_cache.shape[-2] * 2
    x, x_pass = x[..., :rot_dim], x[..., rot_dim:]
    # truncate to support variable sizes
    rope_cache = rope_cache[:, :sq]
    xshaped = x.reshape(b, np, sq, rot_dim // 2, 2)
    rope_cache = rope_cache.view(-1, 1, sq, xshaped.size(3), 2)
    x_out2 = torch.stack(
        [
            xshaped[..., 0] * rope_cache[..., 0] - xshaped[..., 1] * rope_cache[..., 1],
            xshaped[..., 1] * rope_cache[..., 0] + xshaped[..., 0] * rope_cache[..., 1],
        ],
        -1,
    )
    x_out2 = x_out2.flatten(3)
    return torch.cat((x_out2, x_pass), dim=-1)


def chatglm4_attention_forward(
    self, hidden_states, attention_mask, rotary_pos_emb, kv_cache=None, use_cache=True
):
    # hidden_states: [b, sq, h]
    bsz, q_len, _ = hidden_states.size()

    # past_key_value: [bsz, n_kv_head, seq_len, head_dim]
    past_key_value = kv_cache

    n_head = self.num_attention_heads_per_partition
    n_kv_head = self.num_multi_query_groups_per_partition if self.multi_query_attention else n_head
    head_dim = self.hidden_size_per_attention_head

    qkv = self.query_key_value(hidden_states)
    # [bs, q_len, np * 3 * hn] -> [bsz, n_head, seq_len, head_dim]
    qkv = qkv.view(bsz, q_len, n_head + 2 * n_kv_head, head_dim)
    qkv = qkv.transpose(1, 2)

    query_states, key_states, value_states = qkv.split([n_head,
                                                        n_kv_head,
                                                        n_kv_head], dim=1)

    if rotary_pos_emb is not None:
        query_states = apply_rotary_pos_emb(query_states, rotary_pos_emb)
        key_states = apply_rotary_pos_emb(key_states, rotary_pos_emb)

    kv_seq_len = key_states.shape[2]
    if past_key_value is not None:
        kv_seq_len += past_key_value[0].shape[2]

    key_states, value_states = update_past_key_value(
        past_key_value, key_states, value_states,
        kv_seq_len, False, hidden_states.device
    )

    if use_cache:
        past_key_value = (key_states, value_states)
    else:
        past_key_value = None

    # repeat k/v heads if n_kv_heads < n_heads
    key_states = repeat_kv(key_states, n_head // n_kv_head)
    value_states = repeat_kv(value_states, n_head // n_kv_head)

    if query_states.size(2) == key_states.size(2):
        # first token
        from intel_npu_acceleration_library.functional import scaled_dot_product_attention
        attn_output = scaled_dot_product_attention(
            query_states,
            key_states,
            value_states,
            attn_mask=attention_mask,
            is_causal=attention_mask is None and q_len > 1 and bsz == 1,
        )
        attn_weights = None
    else:
        attn_weights = torch.matmul(query_states / math.sqrt(head_dim),
                                    key_states.transpose(2, 3))
        if attention_mask is not None:
            attn_weights = attn_weights + attention_mask
        attn_weights = torch.nn.functional.softmax(attn_weights, dim=-1,
                                                   dtype=torch.float32).to(value_states.dtype)
        attn_output = torch.matmul(attn_weights, value_states)

    # context_layer's shape: [bsz, n_head, seq_len, head_dim] -> [seq_len, bsz, n_head * head_dim]
    attn_output = attn_output.transpose(1, 2).contiguous().view(bsz, q_len, n_head * head_dim)
    output = self.dense(attn_output)

    return output, past_key_value
