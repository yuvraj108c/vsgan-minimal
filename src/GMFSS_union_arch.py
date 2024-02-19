import torch
import torch.nn as nn
import torch.nn.functional as F
import numpy as np
import collections
import cupy
import os
import re
import torch
import typing
from torch.nn.modules.utils import _pair
import math
from .download import check_and_download

device = torch.device("cuda")


class PositionEmbeddingSine(nn.Module):
    """
    This is a more standard version of the position embedding, very similar to the one
    used by the Attention is all you need paper, generalized to work on images.
    """

    def __init__(self, num_pos_feats=64, temperature=10000, normalize=True, scale=None):
        super().__init__()
        self.num_pos_feats = num_pos_feats
        self.temperature = temperature
        self.normalize = normalize
        if scale is not None and normalize is False:
            raise ValueError("normalize should be True if scale is passed")
        if scale is None:
            scale = 2 * math.pi
        self.scale = scale

    def forward(self, x):
        # x = tensor_list.tensors  # [B, C, H, W]
        # mask = tensor_list.mask  # [B, H, W], input with padding, valid as 0
        b, c, h, w = x.size()
        mask = torch.ones((b, h, w), device=x.device)  # [B, H, W]
        y_embed = mask.cumsum(1, dtype=torch.float32)
        x_embed = mask.cumsum(2, dtype=torch.float32)
        if self.normalize:
            eps = 1e-6
            y_embed = y_embed / (y_embed[:, -1:, :] + eps) * self.scale
            x_embed = x_embed / (x_embed[:, :, -1:] + eps) * self.scale

        dim_t = torch.arange(self.num_pos_feats, dtype=torch.float32, device=x.device)
        dim_t = self.temperature ** (2 * (dim_t // 2) / self.num_pos_feats)

        pos_x = x_embed[:, :, :, None] / dim_t
        pos_y = y_embed[:, :, :, None] / dim_t
        pos_x = torch.stack(
            (pos_x[:, :, :, 0::2].sin(), pos_x[:, :, :, 1::2].cos()), dim=4
        ).flatten(3)
        pos_y = torch.stack(
            (pos_y[:, :, :, 0::2].sin(), pos_y[:, :, :, 1::2].cos()), dim=4
        ).flatten(3)
        pos = torch.cat((pos_y, pos_x), dim=3).permute(0, 3, 1, 2)
        return pos


class MultiScaleTridentConv(nn.Module):
    def __init__(
        self,
        in_channels,
        out_channels,
        kernel_size,
        stride=1,
        strides=1,
        paddings=0,
        dilations=1,
        dilation=1,
        groups=1,
        num_branch=1,
        test_branch_idx=-1,
        bias=False,
        norm=None,
        activation=None,
    ):
        super(MultiScaleTridentConv, self).__init__()
        self.in_channels = in_channels
        self.out_channels = out_channels
        self.kernel_size = _pair(kernel_size)
        self.num_branch = num_branch
        self.stride = _pair(stride)
        self.groups = groups
        self.with_bias = bias
        self.dilation = dilation
        if isinstance(paddings, int):
            paddings = [paddings] * self.num_branch
        if isinstance(dilations, int):
            dilations = [dilations] * self.num_branch
        if isinstance(strides, int):
            strides = [strides] * self.num_branch
        self.paddings = [_pair(padding) for padding in paddings]
        self.dilations = [_pair(dilation) for dilation in dilations]
        self.strides = [_pair(stride) for stride in strides]
        self.test_branch_idx = test_branch_idx
        self.norm = norm
        self.activation = activation

        assert len({self.num_branch, len(self.paddings), len(self.strides)}) == 1

        self.weight = nn.Parameter(
            torch.Tensor(out_channels, in_channels // groups, *self.kernel_size)
        )
        if bias:
            self.bias = nn.Parameter(torch.Tensor(out_channels))
        else:
            self.bias = None

        nn.init.kaiming_uniform_(self.weight, nonlinearity="relu")
        if self.bias is not None:
            nn.init.constant_(self.bias, 0)

    def forward(self, inputs):
        num_branch = (
            self.num_branch if self.training or self.test_branch_idx == -1 else 1
        )
        assert len(inputs) == num_branch

        if self.training or self.test_branch_idx == -1:
            outputs = [
                F.conv2d(
                    input,
                    self.weight,
                    self.bias,
                    stride,
                    padding,
                    self.dilation,
                    self.groups,
                )
                for input, stride, padding in zip(inputs, self.strides, self.paddings)
            ]
        else:
            outputs = [
                F.conv2d(
                    inputs[0],
                    self.weight,
                    self.bias,
                    self.strides[self.test_branch_idx]
                    if self.test_branch_idx == -1
                    else self.strides[-1],
                    self.paddings[self.test_branch_idx]
                    if self.test_branch_idx == -1
                    else self.paddings[-1],
                    self.dilation,
                    self.groups,
                )
            ]

        if self.norm is not None:
            outputs = [self.norm(x) for x in outputs]
        if self.activation is not None:
            outputs = [self.activation(x) for x in outputs]
        return outputs


def split_feature(
    feature,
    num_splits=2,
    channel_last=False,
):
    if channel_last:  # [B, H, W, C]
        b, h, w, c = feature.size()
        assert h % num_splits == 0 and w % num_splits == 0

        b_new = b * num_splits * num_splits
        h_new = h // num_splits
        w_new = w // num_splits

        feature = (
            feature.view(b, num_splits, h // num_splits, num_splits, w // num_splits, c)
            .permute(0, 1, 3, 2, 4, 5)
            .reshape(b_new, h_new, w_new, c)
        )  # [B*K*K, H/K, W/K, C]
    else:  # [B, C, H, W]
        b, c, h, w = feature.size()
        assert h % num_splits == 0 and w % num_splits == 0

        b_new = b * num_splits * num_splits
        h_new = h // num_splits
        w_new = w // num_splits

        feature = (
            feature.view(b, c, num_splits, h // num_splits, num_splits, w // num_splits)
            .permute(0, 2, 4, 1, 3, 5)
            .reshape(b_new, c, h_new, w_new)
        )  # [B*K*K, C, H/K, W/K]

    return feature


def merge_splits(
    splits,
    num_splits=2,
    channel_last=False,
):
    if channel_last:  # [B*K*K, H/K, W/K, C]
        b, h, w, c = splits.size()
        new_b = b // num_splits // num_splits

        splits = splits.view(new_b, num_splits, num_splits, h, w, c)
        merge = (
            splits.permute(0, 1, 3, 2, 4, 5)
            .contiguous()
            .view(new_b, num_splits * h, num_splits * w, c)
        )  # [B, H, W, C]
    else:  # [B*K*K, C, H/K, W/K]
        b, c, h, w = splits.size()
        new_b = b // num_splits // num_splits

        splits = splits.view(new_b, num_splits, num_splits, c, h, w)
        merge = (
            splits.permute(0, 3, 1, 4, 2, 5)
            .contiguous()
            .view(new_b, c, num_splits * h, num_splits * w)
        )  # [B, C, H, W]

    return merge


def normalize_img(img0, img1):
    # loaded images are in [0, 255]
    # normalize by ImageNet mean and std
    mean = torch.tensor([0.485, 0.456, 0.406]).view(1, 3, 1, 1).to(img1.device)
    std = torch.tensor([0.229, 0.224, 0.225]).view(1, 3, 1, 1).to(img1.device)
    img0 = (img0 - mean) / std
    img1 = (img1 - mean) / std

    return img0, img1


def feature_add_position(feature0, feature1, attn_splits, feature_channels):
    pos_enc = PositionEmbeddingSine(num_pos_feats=feature_channels // 2)

    if attn_splits > 1:  # add position in splited window
        feature0_splits = split_feature(feature0, num_splits=attn_splits)
        feature1_splits = split_feature(feature1, num_splits=attn_splits)

        position = pos_enc(feature0_splits)

        feature0_splits = feature0_splits + position
        feature1_splits = feature1_splits + position

        feature0 = merge_splits(feature0_splits, num_splits=attn_splits)
        feature1 = merge_splits(feature1_splits, num_splits=attn_splits)
    else:
        position = pos_enc(feature0)

        feature0 = feature0 + position
        feature1 = feature1 + position

    return feature0, feature1


def global_correlation_softmax(
    feature0,
    feature1,
    pred_bidir_flow=False,
):
    # global correlation
    b, c, h, w = feature0.shape
    feature0 = feature0.view(b, c, -1).permute(0, 2, 1)  # [B, H*W, C]
    feature1 = feature1.view(b, c, -1)  # [B, C, H*W]

    correlation = torch.matmul(feature0, feature1).view(b, h, w, h, w) / (
        c**0.5
    )  # [B, H, W, H, W]

    # flow from softmax
    init_grid = coords_grid(b, h, w).to(correlation.device)  # [B, 2, H, W]
    grid = init_grid.view(b, 2, -1).permute(0, 2, 1)  # [B, H*W, 2]

    correlation = correlation.view(b, h * w, h * w)  # [B, H*W, H*W]

    if pred_bidir_flow:
        correlation = torch.cat(
            (correlation, correlation.permute(0, 2, 1)), dim=0
        )  # [2*B, H*W, H*W]
        init_grid = init_grid.repeat(2, 1, 1, 1)  # [2*B, 2, H, W]
        grid = grid.repeat(2, 1, 1)  # [2*B, H*W, 2]
        b = b * 2

    prob = F.softmax(correlation, dim=-1)  # [B, H*W, H*W]

    correspondence = (
        torch.matmul(prob, grid).view(b, h, w, 2).permute(0, 3, 1, 2)
    )  # [B, 2, H, W]

    # when predicting bidirectional flow, flow is the concatenation of forward flow and backward flow
    flow = correspondence - init_grid

    return flow, prob


def local_correlation_softmax(
    feature0,
    feature1,
    local_radius,
    padding_mode="zeros",
):
    b, c, h, w = feature0.size()
    coords_init = coords_grid(b, h, w).to(feature0.device)  # [B, 2, H, W]
    coords = coords_init.view(b, 2, -1).permute(0, 2, 1)  # [B, H*W, 2]

    local_h = 2 * local_radius + 1
    local_w = 2 * local_radius + 1

    window_grid = generate_window_grid(
        -local_radius,
        local_radius,
        -local_radius,
        local_radius,
        local_h,
        local_w,
        device=feature0.device,
    )  # [2R+1, 2R+1, 2]
    window_grid = window_grid.reshape(-1, 2).repeat(b, 1, 1, 1)  # [B, 1, (2R+1)^2, 2]
    sample_coords = coords.unsqueeze(-2) + window_grid  # [B, H*W, (2R+1)^2, 2]

    sample_coords_softmax = sample_coords

    # exclude coords that are out of image space
    valid_x = (sample_coords[:, :, :, 0] >= 0) & (
        sample_coords[:, :, :, 0] < w
    )  # [B, H*W, (2R+1)^2]
    valid_y = (sample_coords[:, :, :, 1] >= 0) & (
        sample_coords[:, :, :, 1] < h
    )  # [B, H*W, (2R+1)^2]

    valid = (
        valid_x & valid_y
    )  # [B, H*W, (2R+1)^2], used to mask out invalid values when softmax

    # normalize coordinates to [-1, 1]
    sample_coords_norm = normalize_coords(sample_coords, h, w)  # [-1, 1]
    window_feature = F.grid_sample(
        feature1, sample_coords_norm, padding_mode=padding_mode, align_corners=True
    ).permute(
        0, 2, 1, 3
    )  # [B, H*W, C, (2R+1)^2]
    feature0_view = feature0.permute(0, 2, 3, 1).view(b, h * w, 1, c)  # [B, H*W, 1, C]

    corr = torch.matmul(feature0_view, window_feature).view(b, h * w, -1) / (
        c**0.5
    )  # [B, H*W, (2R+1)^2]

    # mask invalid locations
    corr[~valid] = -1e9

    prob = F.softmax(corr, -1)  # [B, H*W, (2R+1)^2]

    correspondence = (
        torch.matmul(prob.unsqueeze(-2), sample_coords_softmax)
        .squeeze(-2)
        .view(b, h, w, 2)
        .permute(0, 3, 1, 2)
    )  # [B, 2, H, W]

    flow = correspondence - coords_init
    match_prob = prob

    return flow, match_prob


def single_head_full_attention(q, k, v):
    # q, k, v: [B, L, C]
    assert q.dim() == k.dim() == v.dim() == 3

    scores = torch.matmul(q, k.permute(0, 2, 1)) / (q.size(2) ** 0.5)  # [B, L, L]
    attn = torch.softmax(scores, dim=2)  # [B, L, L]
    out = torch.matmul(attn, v)  # [B, L, C]

    return out


def generate_shift_window_attn_mask(
    input_resolution,
    window_size_h,
    window_size_w,
    shift_size_h,
    shift_size_w,
    device=torch.device("cuda"),
):
    # Ref: https://github.com/microsoft/Swin-Transformer/blob/main/models/swin_transformer.py
    # calculate attention mask for SW-MSA
    h, w = input_resolution
    img_mask = torch.zeros((1, h, w, 1)).to(device)  # 1 H W 1
    h_slices = (
        slice(0, -window_size_h),
        slice(-window_size_h, -shift_size_h),
        slice(-shift_size_h, None),
    )
    w_slices = (
        slice(0, -window_size_w),
        slice(-window_size_w, -shift_size_w),
        slice(-shift_size_w, None),
    )
    cnt = 0
    for h in h_slices:
        for w in w_slices:
            img_mask[:, h, w, :] = cnt
            cnt += 1

    mask_windows = split_feature(
        img_mask, num_splits=input_resolution[-1] // window_size_w, channel_last=True
    )

    mask_windows = mask_windows.view(-1, window_size_h * window_size_w)
    attn_mask = mask_windows.unsqueeze(1) - mask_windows.unsqueeze(2)
    attn_mask = attn_mask.masked_fill(attn_mask != 0, float(-100.0)).masked_fill(
        attn_mask == 0, float(0.0)
    )

    return attn_mask


def single_head_split_window_attention(
    q,
    k,
    v,
    num_splits=1,
    with_shift=False,
    h=None,
    w=None,
    attn_mask=None,
):
    # Ref: https://github.com/microsoft/Swin-Transformer/blob/main/models/swin_transformer.py
    # q, k, v: [B, L, C]
    assert q.dim() == k.dim() == v.dim() == 3

    assert h is not None and w is not None
    assert q.size(1) == h * w

    b, _, c = q.size()

    b_new = b * num_splits * num_splits

    window_size_h = h // num_splits
    window_size_w = w // num_splits

    q = q.view(b, h, w, c)  # [B, H, W, C]
    k = k.view(b, h, w, c)
    v = v.view(b, h, w, c)

    scale_factor = c**0.5

    if with_shift:
        assert attn_mask is not None  # compute once
        shift_size_h = window_size_h // 2
        shift_size_w = window_size_w // 2

        q = torch.roll(q, shifts=(-shift_size_h, -shift_size_w), dims=(1, 2))
        k = torch.roll(k, shifts=(-shift_size_h, -shift_size_w), dims=(1, 2))
        v = torch.roll(v, shifts=(-shift_size_h, -shift_size_w), dims=(1, 2))

    q = split_feature(
        q, num_splits=num_splits, channel_last=True
    )  # [B*K*K, H/K, W/K, C]
    k = split_feature(k, num_splits=num_splits, channel_last=True)
    v = split_feature(v, num_splits=num_splits, channel_last=True)

    scores = (
        torch.matmul(q.view(b_new, -1, c), k.view(b_new, -1, c).permute(0, 2, 1))
        / scale_factor
    )  # [B*K*K, H/K*W/K, H/K*W/K]

    if with_shift:
        scores += attn_mask.repeat(b, 1, 1)

    attn = torch.softmax(scores, dim=-1)

    out = torch.matmul(attn, v.view(b_new, -1, c))  # [B*K*K, H/K*W/K, C]

    out = merge_splits(
        out.view(b_new, h // num_splits, w // num_splits, c),
        num_splits=num_splits,
        channel_last=True,
    )  # [B, H, W, C]

    # shift back
    if with_shift:
        out = torch.roll(out, shifts=(shift_size_h, shift_size_w), dims=(1, 2))

    out = out.view(b, -1, c)

    return out


class TransformerLayer(nn.Module):
    def __init__(
        self,
        d_model=256,
        nhead=1,
        attention_type="swin",
        no_ffn=False,
        ffn_dim_expansion=4,
        with_shift=False,
        **kwargs,
    ):
        super(TransformerLayer, self).__init__()

        self.dim = d_model
        self.nhead = nhead
        self.attention_type = attention_type
        self.no_ffn = no_ffn

        self.with_shift = with_shift

        # multi-head attention
        self.q_proj = nn.Linear(d_model, d_model, bias=False)
        self.k_proj = nn.Linear(d_model, d_model, bias=False)
        self.v_proj = nn.Linear(d_model, d_model, bias=False)

        self.merge = nn.Linear(d_model, d_model, bias=False)

        self.norm1 = nn.LayerNorm(d_model)

        # no ffn after self-attn, with ffn after cross-attn
        if not self.no_ffn:
            in_channels = d_model * 2
            self.mlp = nn.Sequential(
                nn.Linear(in_channels, in_channels * ffn_dim_expansion, bias=False),
                nn.GELU(),
                nn.Linear(in_channels * ffn_dim_expansion, d_model, bias=False),
            )

            self.norm2 = nn.LayerNorm(d_model)

    def forward(
        self,
        source,
        target,
        height=None,
        width=None,
        shifted_window_attn_mask=None,
        attn_num_splits=None,
        **kwargs,
    ):
        # source, target: [B, L, C]
        query, key, value = source, target, target

        # single-head attention
        query = self.q_proj(query)  # [B, L, C]
        key = self.k_proj(key)  # [B, L, C]
        value = self.v_proj(value)  # [B, L, C]

        if self.attention_type == "swin" and attn_num_splits > 1:
            if self.nhead > 1:
                # we observe that multihead attention slows down the speed and increases the memory consumption
                # without bringing obvious performance gains and thus the implementation is removed
                raise NotImplementedError
            else:
                message = single_head_split_window_attention(
                    query,
                    key,
                    value,
                    num_splits=attn_num_splits,
                    with_shift=self.with_shift,
                    h=height,
                    w=width,
                    attn_mask=shifted_window_attn_mask,
                )
        else:
            message = single_head_full_attention(query, key, value)  # [B, L, C]

        message = self.merge(message)  # [B, L, C]
        message = self.norm1(message)

        if not self.no_ffn:
            message = self.mlp(torch.cat([source, message], dim=-1))
            message = self.norm2(message)

        return source + message


class TransformerBlock(nn.Module):
    """self attention + cross attention + FFN"""

    def __init__(
        self,
        d_model=256,
        nhead=1,
        attention_type="swin",
        ffn_dim_expansion=4,
        with_shift=False,
        **kwargs,
    ):
        super(TransformerBlock, self).__init__()

        self.self_attn = TransformerLayer(
            d_model=d_model,
            nhead=nhead,
            attention_type=attention_type,
            no_ffn=True,
            ffn_dim_expansion=ffn_dim_expansion,
            with_shift=with_shift,
        )

        self.cross_attn_ffn = TransformerLayer(
            d_model=d_model,
            nhead=nhead,
            attention_type=attention_type,
            ffn_dim_expansion=ffn_dim_expansion,
            with_shift=with_shift,
        )

    def forward(
        self,
        source,
        target,
        height=None,
        width=None,
        shifted_window_attn_mask=None,
        attn_num_splits=None,
        **kwargs,
    ):
        # source, target: [B, L, C]

        # self attention
        source = self.self_attn(
            source,
            source,
            height=height,
            width=width,
            shifted_window_attn_mask=shifted_window_attn_mask,
            attn_num_splits=attn_num_splits,
        )

        # cross attention and ffn
        source = self.cross_attn_ffn(
            source,
            target,
            height=height,
            width=width,
            shifted_window_attn_mask=shifted_window_attn_mask,
            attn_num_splits=attn_num_splits,
        )

        return source


class FeatureTransformer(nn.Module):
    def __init__(
        self,
        num_layers=6,
        d_model=128,
        nhead=1,
        attention_type="swin",
        ffn_dim_expansion=4,
        **kwargs,
    ):
        super(FeatureTransformer, self).__init__()

        self.attention_type = attention_type

        self.d_model = d_model
        self.nhead = nhead

        self.layers = nn.ModuleList(
            [
                TransformerBlock(
                    d_model=d_model,
                    nhead=nhead,
                    attention_type=attention_type,
                    ffn_dim_expansion=ffn_dim_expansion,
                    with_shift=True
                    if attention_type == "swin" and i % 2 == 1
                    else False,
                )
                for i in range(num_layers)
            ]
        )

        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(
        self,
        feature0,
        feature1,
        attn_num_splits=None,
        **kwargs,
    ):
        b, c, h, w = feature0.shape
        assert self.d_model == c

        feature0 = feature0.flatten(-2).permute(0, 2, 1)  # [B, H*W, C]
        feature1 = feature1.flatten(-2).permute(0, 2, 1)  # [B, H*W, C]

        if self.attention_type == "swin" and attn_num_splits > 1:
            # global and refine use different number of splits
            window_size_h = h // attn_num_splits
            window_size_w = w // attn_num_splits

            # compute attn mask once
            shifted_window_attn_mask = generate_shift_window_attn_mask(
                input_resolution=(h, w),
                window_size_h=window_size_h,
                window_size_w=window_size_w,
                shift_size_h=window_size_h // 2,
                shift_size_w=window_size_w // 2,
                device=feature0.device,
            )  # [K*K, H/K*W/K, H/K*W/K]
        else:
            shifted_window_attn_mask = None

        # concat feature0 and feature1 in batch dimension to compute in parallel
        concat0 = torch.cat((feature0, feature1), dim=0)  # [2B, H*W, C]
        concat1 = torch.cat((feature1, feature0), dim=0)  # [2B, H*W, C]

        for layer in self.layers:
            concat0 = layer(
                concat0,
                concat1,
                height=h,
                width=w,
                shifted_window_attn_mask=shifted_window_attn_mask,
                attn_num_splits=attn_num_splits,
            )

            # update feature1
            concat1 = torch.cat(concat0.chunk(chunks=2, dim=0)[::-1], dim=0)

        feature0, feature1 = concat0.chunk(chunks=2, dim=0)  # [B, H*W, C]

        # reshape back
        feature0 = (
            feature0.view(b, h, w, c).permute(0, 3, 1, 2).contiguous()
        )  # [B, C, H, W]
        feature1 = (
            feature1.view(b, h, w, c).permute(0, 3, 1, 2).contiguous()
        )  # [B, C, H, W]

        return feature0, feature1


class FeatureFlowAttention(nn.Module):
    """
    flow propagation with self-attention on feature
    query: feature0, key: feature0, value: flow
    """

    def __init__(
        self,
        in_channels,
        **kwargs,
    ):
        super(FeatureFlowAttention, self).__init__()

        self.q_proj = nn.Linear(in_channels, in_channels)
        self.k_proj = nn.Linear(in_channels, in_channels)

        for p in self.parameters():
            if p.dim() > 1:
                nn.init.xavier_uniform_(p)

    def forward(
        self,
        feature0,
        flow,
        local_window_attn=False,
        local_window_radius=1,
        **kwargs,
    ):
        # q, k: feature [B, C, H, W], v: flow [B, 2, H, W]
        if local_window_attn:
            return self.forward_local_window_attn(
                feature0, flow, local_window_radius=local_window_radius
            )

        b, c, h, w = feature0.size()

        query = feature0.view(b, c, h * w).permute(0, 2, 1)  # [B, H*W, C]

        # a note: the ``correct'' implementation should be:
        # ``query = self.q_proj(query), key = self.k_proj(query)''
        # this problem is observed while cleaning up the code
        # however, this doesn't affect the performance since the projection is a linear operation,
        # thus the two projection matrices for key can be merged
        # so I just leave it as is in order to not re-train all models :)
        query = self.q_proj(query)  # [B, H*W, C]
        key = self.k_proj(query)  # [B, H*W, C]

        value = flow.view(b, flow.size(1), h * w).permute(0, 2, 1)  # [B, H*W, 2]

        scores = torch.matmul(query, key.permute(0, 2, 1)) / (c**0.5)  # [B, H*W, H*W]
        prob = torch.softmax(scores, dim=-1)

        out = torch.matmul(prob, value)  # [B, H*W, 2]
        out = out.view(b, h, w, value.size(-1)).permute(0, 3, 1, 2)  # [B, 2, H, W]

        return out

    def forward_local_window_attn(
        self,
        feature0,
        flow,
        local_window_radius=1,
    ):
        assert flow.size(1) == 2
        assert local_window_radius > 0

        b, c, h, w = feature0.size()

        feature0_reshape = self.q_proj(
            feature0.view(b, c, -1).permute(0, 2, 1)
        ).reshape(
            b * h * w, 1, c
        )  # [B*H*W, 1, C]

        kernel_size = 2 * local_window_radius + 1

        feature0_proj = (
            self.k_proj(feature0.view(b, c, -1).permute(0, 2, 1))
            .permute(0, 2, 1)
            .reshape(b, c, h, w)
        )

        feature0_window = F.unfold(
            feature0_proj, kernel_size=kernel_size, padding=local_window_radius
        )  # [B, C*(2R+1)^2), H*W]

        feature0_window = (
            feature0_window.view(b, c, kernel_size**2, h, w)
            .permute(0, 3, 4, 1, 2)
            .reshape(b * h * w, c, kernel_size**2)
        )  # [B*H*W, C, (2R+1)^2]

        flow_window = F.unfold(
            flow, kernel_size=kernel_size, padding=local_window_radius
        )  # [B, 2*(2R+1)^2), H*W]

        flow_window = (
            flow_window.view(b, 2, kernel_size**2, h, w)
            .permute(0, 3, 4, 2, 1)
            .reshape(b * h * w, kernel_size**2, 2)
        )  # [B*H*W, (2R+1)^2, 2]

        scores = torch.matmul(feature0_reshape, feature0_window) / (
            c**0.5
        )  # [B*H*W, 1, (2R+1)^2]

        prob = torch.softmax(scores, dim=-1)

        out = (
            torch.matmul(prob, flow_window)
            .view(b, h, w, 2)
            .permute(0, 3, 1, 2)
            .contiguous()
        )  # [B, 2, H, W]

        return out


class ResidualBlockClass(nn.Module):
    def __init__(
        self,
        in_planes,
        planes,
        norm_layer=nn.InstanceNorm2d,
        stride=1,
        dilation=1,
    ):
        super(ResidualBlockClass, self).__init__()

        self.conv1 = nn.Conv2d(
            in_planes,
            planes,
            kernel_size=3,
            dilation=dilation,
            padding=dilation,
            stride=stride,
            bias=False,
        )
        self.conv2 = nn.Conv2d(
            planes,
            planes,
            kernel_size=3,
            dilation=dilation,
            padding=dilation,
            bias=False,
        )
        self.relu = nn.ReLU(inplace=True)

        self.norm1 = norm_layer(planes)
        self.norm2 = norm_layer(planes)
        if not stride == 1 or in_planes != planes:
            self.norm3 = norm_layer(planes)

        if stride == 1 and in_planes == planes:
            self.downsample = None
        else:
            self.downsample = nn.Sequential(
                nn.Conv2d(in_planes, planes, kernel_size=1, stride=stride), self.norm3
            )

    def forward(self, x):
        y = x
        y = self.relu(self.norm1(self.conv1(y)))
        y = self.relu(self.norm2(self.conv2(y)))

        if self.downsample is not None:
            x = self.downsample(x)

        return self.relu(x + y)


class CNNEncoder(nn.Module):
    def __init__(
        self,
        output_dim=128,
        norm_layer=nn.InstanceNorm2d,
        num_output_scales=1,
        **kwargs,
    ):
        super(CNNEncoder, self).__init__()
        self.num_branch = num_output_scales

        feature_dims = [64, 96, 128]

        self.conv1 = nn.Conv2d(
            3, feature_dims[0], kernel_size=7, stride=2, padding=3, bias=False
        )  # 1/2
        self.norm1 = norm_layer(feature_dims[0])
        self.relu1 = nn.ReLU(inplace=True)

        self.in_planes = feature_dims[0]
        self.layer1 = self._make_layer(
            feature_dims[0], stride=1, norm_layer=norm_layer
        )  # 1/2
        self.layer2 = self._make_layer(
            feature_dims[1], stride=2, norm_layer=norm_layer
        )  # 1/4

        # highest resolution 1/4 or 1/8
        stride = 2 if num_output_scales == 1 else 1
        self.layer3 = self._make_layer(
            feature_dims[2],
            stride=stride,
            norm_layer=norm_layer,
        )  # 1/4 or 1/8

        self.conv2 = nn.Conv2d(feature_dims[2], output_dim, 1, 1, 0)

        if self.num_branch > 1:
            if self.num_branch == 4:
                strides = (1, 2, 4, 8)
            elif self.num_branch == 3:
                strides = (1, 2, 4)
            elif self.num_branch == 2:
                strides = (1, 2)
            else:
                raise ValueError

            self.trident_conv = MultiScaleTridentConv(
                output_dim,
                output_dim,
                kernel_size=3,
                strides=strides,
                paddings=1,
                num_branch=self.num_branch,
            )

        for m in self.modules():
            if isinstance(m, nn.Conv2d):
                nn.init.kaiming_normal_(m.weight, mode="fan_out", nonlinearity="relu")
            elif isinstance(m, (nn.BatchNorm2d, nn.InstanceNorm2d, nn.GroupNorm)):
                if m.weight is not None:
                    nn.init.constant_(m.weight, 1)
                if m.bias is not None:
                    nn.init.constant_(m.bias, 0)

    def _make_layer(self, dim, stride=1, dilation=1, norm_layer=nn.InstanceNorm2d):
        layer1 = ResidualBlockClass(
            self.in_planes, dim, norm_layer=norm_layer, stride=stride, dilation=dilation
        )
        layer2 = ResidualBlockClass(
            dim, dim, norm_layer=norm_layer, stride=1, dilation=dilation
        )

        layers = (layer1, layer2)

        self.in_planes = dim
        return nn.Sequential(*layers)

    def forward(self, x):
        x = self.conv1(x)
        x = self.norm1(x)
        x = self.relu1(x)

        x = self.layer1(x)  # 1/2
        x = self.layer2(x)  # 1/4
        x = self.layer3(x)  # 1/8 or 1/4

        x = self.conv2(x)

        if self.num_branch > 1:
            out = self.trident_conv([x] * self.num_branch)  # high to low res
        else:
            out = [x]

        return out


class GMFlow(nn.Module):
    def __init__(
        self,
        num_scales=2,
        upsample_factor=4,
        feature_channels=128,
        attention_type="swin",
        num_transformer_layers=6,
        ffn_dim_expansion=4,
        num_head=1,
        partial_fp16=True,
        **kwargs,
    ):
        super(GMFlow, self).__init__()

        self.num_scales = num_scales
        self.feature_channels = feature_channels
        self.upsample_factor = upsample_factor
        self.attention_type = attention_type
        self.num_transformer_layers = num_transformer_layers
        self.partial_fp16 = partial_fp16

        # CNN backbone
        self.backbone = CNNEncoder(
            output_dim=feature_channels, num_output_scales=num_scales
        )

        # Transformer
        self.transformer = FeatureTransformer(
            num_layers=num_transformer_layers,
            d_model=feature_channels,
            nhead=num_head,
            attention_type=attention_type,
            ffn_dim_expansion=ffn_dim_expansion,
        )
        if self.partial_fp16:
            self.transformer = self.transformer.half()

        # flow propagation with self-attn
        self.feature_flow_attn = FeatureFlowAttention(in_channels=feature_channels)
        if self.partial_fp16:
            self.feature_flow_attn = self.feature_flow_attn.half()

        # convex upsampling: concat feature0 and flow as input
        self.upsampler = nn.Sequential(
            nn.Conv2d(2 + feature_channels, 256, 3, 1, 1),
            nn.ReLU(inplace=True),
            nn.Conv2d(256, upsample_factor**2 * 9, 1, 1, 0),
        )
        if self.partial_fp16:
            self.upsampler = self.upsampler.half()

    def extract_feature(self, img0, img1):
        concat = torch.cat((img0, img1), dim=0)  # [2B, C, H, W]

        features = self.backbone(
            concat
        )  # list of [2B, C, H, W], resolution from high to low

        # reverse: resolution from low to high
        features = features[::-1]

        feature0, feature1 = [], []

        for i in range(len(features)):
            feature = features[i]
            chunks = torch.chunk(feature, 2, 0)  # tuple
            feature0.append(chunks[0])
            feature1.append(chunks[1])

        return feature0, feature1

    def upsample_flow(
        self,
        flow,
        feature,
        bilinear=False,
        upsample_factor=8,
    ):
        if bilinear:
            up_flow = (
                F.interpolate(
                    flow,
                    scale_factor=upsample_factor,
                    mode="bilinear",
                    align_corners=True,
                )
                * upsample_factor
            )

        else:
            # convex upsampling
            concat = torch.cat((flow, feature), dim=1)

            if self.partial_fp16:
                concat = concat.half()

            mask = self.upsampler(concat)

            b, flow_channel, h, w = flow.shape
            mask = mask.view(
                b, 1, 9, self.upsample_factor, self.upsample_factor, h, w
            )  # [B, 1, 9, K, K, H, W]
            mask = torch.softmax(mask, dim=2)

            up_flow = F.unfold(self.upsample_factor * flow, [3, 3], padding=1)
            up_flow = up_flow.view(
                b, flow_channel, 9, 1, 1, h, w
            )  # [B, 2, 9, 1, 1, H, W]

            up_flow = torch.sum(mask * up_flow, dim=2)  # [B, 2, K, K, H, W]
            up_flow = up_flow.permute(0, 1, 4, 2, 5, 3)  # [B, 2, K, H, K, W]
            up_flow = up_flow.reshape(
                b, flow_channel, self.upsample_factor * h, self.upsample_factor * w
            )  # [B, 2, K*H, K*W]

            if self.partial_fp16:
                up_flow = up_flow.float()

        return up_flow

    def forward(
        self,
        img0,
        img1,
        attn_splits_list=[2, 8],
        corr_radius_list=[-1, 4],
        prop_radius_list=[-1, 1],
        pred_bidir_flow=False,
        **kwargs,
    ):
        img0, img1 = normalize_img(img0, img1)  # [B, 3, H, W]

        # resolution low to high
        feature0_list, feature1_list = self.extract_feature(
            img0, img1
        )  # list of features

        flow = None

        assert (
            len(attn_splits_list)
            == len(corr_radius_list)
            == len(prop_radius_list)
            == self.num_scales
        )

        for scale_idx in range(self.num_scales):
            feature0, feature1 = feature0_list[scale_idx], feature1_list[scale_idx]

            if pred_bidir_flow and scale_idx > 0:
                # predicting bidirectional flow with refinement
                feature0, feature1 = torch.cat((feature0, feature1), dim=0), torch.cat(
                    (feature1, feature0), dim=0
                )

            upsample_factor = self.upsample_factor * (
                2 ** (self.num_scales - 1 - scale_idx)
            )

            if scale_idx > 0:
                flow = (
                    F.interpolate(
                        flow, scale_factor=2, mode="bilinear", align_corners=True
                    )
                    * 2
                )

            if flow is not None:
                flow = flow.detach()
                feature1 = flow_warp(feature1, flow)  # [B, C, H, W]

            attn_splits = attn_splits_list[scale_idx]
            corr_radius = corr_radius_list[scale_idx]
            prop_radius = prop_radius_list[scale_idx]

            # add position to features
            feature0, feature1 = feature_add_position(
                feature0, feature1, attn_splits, self.feature_channels
            )

            # Transformer

            if self.partial_fp16:
                feature0 = feature0.half()
                feature1 = feature1.half()

            feature0, feature1 = self.transformer(
                feature0, feature1, attn_num_splits=attn_splits
            )

            if self.partial_fp16:
                feature0 = feature0.float()
                feature1 = feature1.float()

            # correlation and softmax
            if corr_radius == -1:  # global matching
                flow_pred = global_correlation_softmax(
                    feature0, feature1, pred_bidir_flow
                )[0]
            else:  # local matching
                flow_pred = local_correlation_softmax(feature0, feature1, corr_radius)[
                    0
                ]

            # flow or residual flow
            flow = flow + flow_pred if flow is not None else flow_pred

            # upsample to the original resolution for supervison
            if (
                self.training
            ):  # only need to upsample intermediate flow predictions at training time
                flow_bilinear = self.upsample_flow(
                    flow, None, bilinear=True, upsample_factor=upsample_factor
                )

            # flow propagation with self-attn
            if pred_bidir_flow and scale_idx == 0:
                feature0 = torch.cat(
                    (feature0, feature1), dim=0
                )  # [2*B, C, H, W] for propagation

            if self.partial_fp16:
                feature0 = feature0.half()
                flow = flow.half()
            flow = self.feature_flow_attn(
                feature0,
                flow.detach(),
                local_window_attn=prop_radius > 0,
                local_window_radius=prop_radius,
            )
            if self.partial_fp16:
                flow = flow.float()

            # bilinear upsampling at training time except the last one
            if self.training and scale_idx < self.num_scales - 1:
                flow_up = self.upsample_flow(
                    flow, feature0, bilinear=True, upsample_factor=upsample_factor
                )

            if scale_idx == self.num_scales - 1:
                flow_up = self.upsample_flow(flow, feature0)

        return flow_up


##########################################################


objCudacache = {}


def cuda_int32(intIn: int):
    return cupy.int32(intIn)


# end


def cuda_float32(fltIn: float):
    return cupy.float32(fltIn)


# end


def cuda_kernel(strFunction: str, strKernel: str, objVariables: typing.Dict):
    if "device" not in objCudacache:
        objCudacache["device"] = torch.cuda.get_device_name()
    # end

    strKey = strFunction

    for strVariable in objVariables:
        objValue = objVariables[strVariable]

        strKey += strVariable

        if objValue is None:
            continue

        elif type(objValue) == int:
            strKey += str(objValue)

        elif type(objValue) == float:
            strKey += str(objValue)

        elif type(objValue) == bool:
            strKey += str(objValue)

        elif type(objValue) == str:
            strKey += objValue

        elif type(objValue) == torch.Tensor:
            strKey += str(objValue.dtype)
            strKey += str(objValue.shape)
            strKey += str(objValue.stride())

        elif True:
            print(strVariable, type(objValue))
            assert False

        # end
    # end

    strKey += objCudacache["device"]

    if strKey not in objCudacache:
        for strVariable in objVariables:
            objValue = objVariables[strVariable]

            if objValue is None:
                continue

            elif type(objValue) == int:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == float:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == bool:
                strKernel = strKernel.replace("{{" + strVariable + "}}", str(objValue))

            elif type(objValue) == str:
                strKernel = strKernel.replace("{{" + strVariable + "}}", objValue)

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.uint8:
                strKernel = strKernel.replace("{{type}}", "unsigned char")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float16:
                strKernel = strKernel.replace("{{type}}", "half")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float32:
                strKernel = strKernel.replace("{{type}}", "float")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.float64:
                strKernel = strKernel.replace("{{type}}", "double")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.int32:
                strKernel = strKernel.replace("{{type}}", "int")

            elif type(objValue) == torch.Tensor and objValue.dtype == torch.int64:
                strKernel = strKernel.replace("{{type}}", "long")

            elif type(objValue) == torch.Tensor:
                print(strVariable, objValue.dtype)
                assert False

            elif True:
                print(strVariable, type(objValue))
                assert False

            # end
        # end

        while True:
            objMatch = re.search("(SIZE_)([0-4])(\()([^\)]*)(\))", strKernel)

            if objMatch is None:
                break
            # end

            intArg = int(objMatch.group(2))

            strTensor = objMatch.group(4)
            intSizes = objVariables[strTensor].size()

            strKernel = strKernel.replace(
                objMatch.group(),
                str(
                    intSizes[intArg]
                    if torch.is_tensor(intSizes[intArg]) == False
                    else intSizes[intArg].item()
                ),
            )
        # end

        while True:
            objMatch = re.search("(OFFSET_)([0-4])(\()", strKernel)

            if objMatch is None:
                break
            # end

            intStart = objMatch.span()[1]
            intStop = objMatch.span()[1]
            intParentheses = 1

            while True:
                intParentheses += 1 if strKernel[intStop] == "(" else 0
                intParentheses -= 1 if strKernel[intStop] == ")" else 0

                if intParentheses == 0:
                    break
                # end

                intStop += 1
            # end

            intArgs = int(objMatch.group(2))
            strArgs = strKernel[intStart:intStop].split(",")

            assert intArgs == len(strArgs) - 1

            strTensor = strArgs[0]
            intStrides = objVariables[strTensor].stride()

            strIndex = []

            for intArg in range(intArgs):
                strIndex.append(
                    "(("
                    + strArgs[intArg + 1].replace("{", "(").replace("}", ")").strip()
                    + ")*"
                    + str(
                        intStrides[intArg]
                        if torch.is_tensor(intStrides[intArg]) == False
                        else intStrides[intArg].item()
                    )
                    + ")"
                )
            # end

            strKernel = strKernel.replace(
                "OFFSET_" + str(intArgs) + "(" + strKernel[intStart:intStop] + ")",
                "(" + str.join("+", strIndex) + ")",
            )
        # end

        while True:
            objMatch = re.search("(VALUE_)([0-4])(\()", strKernel)

            if objMatch is None:
                break
            # end

            intStart = objMatch.span()[1]
            intStop = objMatch.span()[1]
            intParentheses = 1

            while True:
                intParentheses += 1 if strKernel[intStop] == "(" else 0
                intParentheses -= 1 if strKernel[intStop] == ")" else 0

                if intParentheses == 0:
                    break
                # end

                intStop += 1
            # end

            intArgs = int(objMatch.group(2))
            strArgs = strKernel[intStart:intStop].split(",")

            assert intArgs == len(strArgs) - 1

            strTensor = strArgs[0]
            intStrides = objVariables[strTensor].stride()

            strIndex = []

            for intArg in range(intArgs):
                strIndex.append(
                    "(("
                    + strArgs[intArg + 1].replace("{", "(").replace("}", ")").strip()
                    + ")*"
                    + str(
                        intStrides[intArg]
                        if torch.is_tensor(intStrides[intArg]) == False
                        else intStrides[intArg].item()
                    )
                    + ")"
                )
            # end

            strKernel = strKernel.replace(
                "VALUE_" + str(intArgs) + "(" + strKernel[intStart:intStop] + ")",
                strTensor + "[" + str.join("+", strIndex) + "]",
            )
        # end

        objCudacache[strKey] = {"strFunction": strFunction, "strKernel": strKernel}
    # end

    return strKey


# end


@cupy.memoize(for_each_device=True)
def cuda_launch(strKey: str):
    if "CUDA_HOME" not in os.environ:
        os.environ["CUDA_HOME"] = cupy.cuda.get_cuda_path()
    # end

    return cupy.cuda.compile_with_cache(
        objCudacache[strKey]["strKernel"],
        tuple(
            [
                "-I " + os.environ["CUDA_HOME"],
                "-I " + os.environ["CUDA_HOME"] + "/include",
            ]
        ),
    ).get_function(objCudacache[strKey]["strFunction"])


# end


##########################f################################


def warp(
    tenIn: torch.Tensor, tenFlow: torch.Tensor, tenMetric: torch.Tensor, strMode: str
):
    assert strMode.split("-")[0] in ["sum", "avg", "linear", "soft"]

    if strMode == "sum":
        assert tenMetric is None
    if strMode == "avg":
        assert tenMetric is None
    if strMode.split("-")[0] == "linear":
        assert tenMetric is not None
    if strMode.split("-")[0] == "soft":
        assert tenMetric is not None

    if strMode == "avg":
        tenIn = torch.cat(
            [
                tenIn,
                tenIn.new_ones([tenIn.shape[0], 1, tenIn.shape[2], tenIn.shape[3]]),
            ],
            1,
        )

    elif strMode.split("-")[0] == "linear":
        tenIn = torch.cat([tenIn * tenMetric, tenMetric], 1)

    elif strMode.split("-")[0] == "soft":
        tenIn = torch.cat([tenIn * tenMetric.exp(), tenMetric.exp()], 1)

    # end

    tenOut = softsplat_func.apply(tenIn, tenFlow)

    if strMode.split("-")[0] in ["avg", "linear", "soft"]:
        tenNormalize = tenOut[:, -1:, :, :]

        if len(strMode.split("-")) == 1:
            tenNormalize = tenNormalize + 0.0000001

        elif strMode.split("-")[1] == "addeps":
            tenNormalize = tenNormalize + 0.0000001

        elif strMode.split("-")[1] == "zeroeps":
            tenNormalize[tenNormalize == 0.0] = 1.0

        elif strMode.split("-")[1] == "clipeps":
            tenNormalize = tenNormalize.clip(0.0000001, None)

        # end

        tenOut = tenOut[:, :-1, :, :] / tenNormalize
    # end

    return tenOut


# end


class softsplat_func(torch.autograd.Function):
    @staticmethod
    @torch.cuda.amp.custom_fwd(cast_inputs=torch.float32)
    def forward(self, tenIn, tenFlow):
        tenOut = tenIn.new_zeros(
            [tenIn.shape[0], tenIn.shape[1], tenIn.shape[2], tenIn.shape[3]]
        )

        if tenIn.is_cuda == True:
            cuda_launch(
                cuda_kernel(
                    "softsplat_out",
                    """
                extern "C" __global__ void __launch_bounds__(512) softsplat_out(
                    const int n,
                    const {{type}}* __restrict__ tenIn,
                    const {{type}}* __restrict__ tenFlow,
                    {{type}}* __restrict__ tenOut
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenOut) / SIZE_2(tenOut) / SIZE_1(tenOut) ) % SIZE_0(tenOut);
                    const int intC = ( intIndex / SIZE_3(tenOut) / SIZE_2(tenOut)                  ) % SIZE_1(tenOut);
                    const int intY = ( intIndex / SIZE_3(tenOut)                                   ) % SIZE_2(tenOut);
                    const int intX = ( intIndex                                                    ) % SIZE_3(tenOut);

                    assert(SIZE_1(tenFlow) == 2);

                    {{type}} fltX = ({{type}}) (intX) + VALUE_4(tenFlow, intN, 0, intY, intX);
                    {{type}} fltY = ({{type}}) (intY) + VALUE_4(tenFlow, intN, 1, intY, intX);

                    if (isfinite(fltX) == false) { return; }
                    if (isfinite(fltY) == false) { return; }

                    {{type}} fltIn = VALUE_4(tenIn, intN, intC, intY, intX);

                    int intNorthwestX = (int) (floor(fltX));
                    int intNorthwestY = (int) (floor(fltY));
                    int intNortheastX = intNorthwestX + 1;
                    int intNortheastY = intNorthwestY;
                    int intSouthwestX = intNorthwestX;
                    int intSouthwestY = intNorthwestY + 1;
                    int intSoutheastX = intNorthwestX + 1;
                    int intSoutheastY = intNorthwestY + 1;

                    {{type}} fltNorthwest = (({{type}}) (intSoutheastX) - fltX) * (({{type}}) (intSoutheastY) - fltY);
                    {{type}} fltNortheast = (fltX - ({{type}}) (intSouthwestX)) * (({{type}}) (intSouthwestY) - fltY);
                    {{type}} fltSouthwest = (({{type}}) (intNortheastX) - fltX) * (fltY - ({{type}}) (intNortheastY));
                    {{type}} fltSoutheast = (fltX - ({{type}}) (intNorthwestX)) * (fltY - ({{type}}) (intNorthwestY));

                    if ((intNorthwestX >= 0) && (intNorthwestX < SIZE_3(tenOut)) && (intNorthwestY >= 0) && (intNorthwestY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intNorthwestY, intNorthwestX)], fltIn * fltNorthwest);
                    }

                    if ((intNortheastX >= 0) && (intNortheastX < SIZE_3(tenOut)) && (intNortheastY >= 0) && (intNortheastY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intNortheastY, intNortheastX)], fltIn * fltNortheast);
                    }

                    if ((intSouthwestX >= 0) && (intSouthwestX < SIZE_3(tenOut)) && (intSouthwestY >= 0) && (intSouthwestY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intSouthwestY, intSouthwestX)], fltIn * fltSouthwest);
                    }

                    if ((intSoutheastX >= 0) && (intSoutheastX < SIZE_3(tenOut)) && (intSoutheastY >= 0) && (intSoutheastY < SIZE_2(tenOut))) {
                        atomicAdd(&tenOut[OFFSET_4(tenOut, intN, intC, intSoutheastY, intSoutheastX)], fltIn * fltSoutheast);
                    }
                } }
            """,
                    {"tenIn": tenIn, "tenFlow": tenFlow, "tenOut": tenOut},
                )
            )(
                grid=tuple([int((tenOut.nelement() + 512 - 1) / 512), 1, 1]),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(tenOut.nelement()),
                    tenIn.data_ptr(),
                    tenFlow.data_ptr(),
                    tenOut.data_ptr(),
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )

        elif tenIn.is_cuda != True:
            assert False

        # end

        self.save_for_backward(tenIn, tenFlow)

        return tenOut

    # end

    @staticmethod
    @torch.cuda.amp.custom_bwd
    def backward(self, tenOutgrad):
        tenIn, tenFlow = self.saved_tensors

        tenOutgrad = tenOutgrad.contiguous()
        assert tenOutgrad.is_cuda == True

        tenIngrad = (
            tenIn.new_empty(
                [tenIn.shape[0], tenIn.shape[1], tenIn.shape[2], tenIn.shape[3]]
            )
            if self.needs_input_grad[0] == True
            else None
        )
        tenFlowgrad = (
            tenFlow.new_empty(
                [tenFlow.shape[0], tenFlow.shape[1], tenFlow.shape[2], tenFlow.shape[3]]
            )
            if self.needs_input_grad[1] == True
            else None
        )

        if tenIngrad is not None:
            cuda_launch(
                cuda_kernel(
                    "softsplat_ingrad",
                    """
                extern "C" __global__ void __launch_bounds__(512) softsplat_ingrad(
                    const int n,
                    const {{type}}* __restrict__ tenIn,
                    const {{type}}* __restrict__ tenFlow,
                    const {{type}}* __restrict__ tenOutgrad,
                    {{type}}* __restrict__ tenIngrad,
                    {{type}}* __restrict__ tenFlowgrad
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenIngrad) / SIZE_2(tenIngrad) / SIZE_1(tenIngrad) ) % SIZE_0(tenIngrad);
                    const int intC = ( intIndex / SIZE_3(tenIngrad) / SIZE_2(tenIngrad)                     ) % SIZE_1(tenIngrad);
                    const int intY = ( intIndex / SIZE_3(tenIngrad)                                         ) % SIZE_2(tenIngrad);
                    const int intX = ( intIndex                                                             ) % SIZE_3(tenIngrad);

                    assert(SIZE_1(tenFlow) == 2);

                    {{type}} fltIngrad = 0.0f;

                    {{type}} fltX = ({{type}}) (intX) + VALUE_4(tenFlow, intN, 0, intY, intX);
                    {{type}} fltY = ({{type}}) (intY) + VALUE_4(tenFlow, intN, 1, intY, intX);

                    if (isfinite(fltX) == false) { return; }
                    if (isfinite(fltY) == false) { return; }

                    int intNorthwestX = (int) (floor(fltX));
                    int intNorthwestY = (int) (floor(fltY));
                    int intNortheastX = intNorthwestX + 1;
                    int intNortheastY = intNorthwestY;
                    int intSouthwestX = intNorthwestX;
                    int intSouthwestY = intNorthwestY + 1;
                    int intSoutheastX = intNorthwestX + 1;
                    int intSoutheastY = intNorthwestY + 1;

                    {{type}} fltNorthwest = (({{type}}) (intSoutheastX) - fltX) * (({{type}}) (intSoutheastY) - fltY);
                    {{type}} fltNortheast = (fltX - ({{type}}) (intSouthwestX)) * (({{type}}) (intSouthwestY) - fltY);
                    {{type}} fltSouthwest = (({{type}}) (intNortheastX) - fltX) * (fltY - ({{type}}) (intNortheastY));
                    {{type}} fltSoutheast = (fltX - ({{type}}) (intNorthwestX)) * (fltY - ({{type}}) (intNorthwestY));

                    if ((intNorthwestX >= 0) && (intNorthwestX < SIZE_3(tenOutgrad)) && (intNorthwestY >= 0) && (intNorthwestY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intNorthwestY, intNorthwestX) * fltNorthwest;
                    }

                    if ((intNortheastX >= 0) && (intNortheastX < SIZE_3(tenOutgrad)) && (intNortheastY >= 0) && (intNortheastY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intNortheastY, intNortheastX) * fltNortheast;
                    }

                    if ((intSouthwestX >= 0) && (intSouthwestX < SIZE_3(tenOutgrad)) && (intSouthwestY >= 0) && (intSouthwestY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intSouthwestY, intSouthwestX) * fltSouthwest;
                    }

                    if ((intSoutheastX >= 0) && (intSoutheastX < SIZE_3(tenOutgrad)) && (intSoutheastY >= 0) && (intSoutheastY < SIZE_2(tenOutgrad))) {
                        fltIngrad += VALUE_4(tenOutgrad, intN, intC, intSoutheastY, intSoutheastX) * fltSoutheast;
                    }

                    tenIngrad[intIndex] = fltIngrad;
                } }
            """,
                    {
                        "tenIn": tenIn,
                        "tenFlow": tenFlow,
                        "tenOutgrad": tenOutgrad,
                        "tenIngrad": tenIngrad,
                        "tenFlowgrad": tenFlowgrad,
                    },
                )
            )(
                grid=tuple([int((tenIngrad.nelement() + 512 - 1) / 512), 1, 1]),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(tenIngrad.nelement()),
                    tenIn.data_ptr(),
                    tenFlow.data_ptr(),
                    tenOutgrad.data_ptr(),
                    tenIngrad.data_ptr(),
                    None,
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )
        # end

        if tenFlowgrad is not None:
            cuda_launch(
                cuda_kernel(
                    "softsplat_flowgrad",
                    """
                extern "C" __global__ void __launch_bounds__(512) softsplat_flowgrad(
                    const int n,
                    const {{type}}* __restrict__ tenIn,
                    const {{type}}* __restrict__ tenFlow,
                    const {{type}}* __restrict__ tenOutgrad,
                    {{type}}* __restrict__ tenIngrad,
                    {{type}}* __restrict__ tenFlowgrad
                ) { for (int intIndex = (blockIdx.x * blockDim.x) + threadIdx.x; intIndex < n; intIndex += blockDim.x * gridDim.x) {
                    const int intN = ( intIndex / SIZE_3(tenFlowgrad) / SIZE_2(tenFlowgrad) / SIZE_1(tenFlowgrad) ) % SIZE_0(tenFlowgrad);
                    const int intC = ( intIndex / SIZE_3(tenFlowgrad) / SIZE_2(tenFlowgrad)                       ) % SIZE_1(tenFlowgrad);
                    const int intY = ( intIndex / SIZE_3(tenFlowgrad)                                             ) % SIZE_2(tenFlowgrad);
                    const int intX = ( intIndex                                                                   ) % SIZE_3(tenFlowgrad);

                    assert(SIZE_1(tenFlow) == 2);

                    {{type}} fltFlowgrad = 0.0f;

                    {{type}} fltX = ({{type}}) (intX) + VALUE_4(tenFlow, intN, 0, intY, intX);
                    {{type}} fltY = ({{type}}) (intY) + VALUE_4(tenFlow, intN, 1, intY, intX);

                    if (isfinite(fltX) == false) { return; }
                    if (isfinite(fltY) == false) { return; }

                    int intNorthwestX = (int) (floor(fltX));
                    int intNorthwestY = (int) (floor(fltY));
                    int intNortheastX = intNorthwestX + 1;
                    int intNortheastY = intNorthwestY;
                    int intSouthwestX = intNorthwestX;
                    int intSouthwestY = intNorthwestY + 1;
                    int intSoutheastX = intNorthwestX + 1;
                    int intSoutheastY = intNorthwestY + 1;

                    {{type}} fltNorthwest = 0.0f;
                    {{type}} fltNortheast = 0.0f;
                    {{type}} fltSouthwest = 0.0f;
                    {{type}} fltSoutheast = 0.0f;

                    if (intC == 0) {
                        fltNorthwest = (({{type}}) (-1.0f)) * (({{type}}) (intSoutheastY) - fltY);
                        fltNortheast = (({{type}}) (+1.0f)) * (({{type}}) (intSouthwestY) - fltY);
                        fltSouthwest = (({{type}}) (-1.0f)) * (fltY - ({{type}}) (intNortheastY));
                        fltSoutheast = (({{type}}) (+1.0f)) * (fltY - ({{type}}) (intNorthwestY));

                    } else if (intC == 1) {
                        fltNorthwest = (({{type}}) (intSoutheastX) - fltX) * (({{type}}) (-1.0f));
                        fltNortheast = (fltX - ({{type}}) (intSouthwestX)) * (({{type}}) (-1.0f));
                        fltSouthwest = (({{type}}) (intNortheastX) - fltX) * (({{type}}) (+1.0f));
                        fltSoutheast = (fltX - ({{type}}) (intNorthwestX)) * (({{type}}) (+1.0f));

                    }

                    for (int intChannel = 0; intChannel < SIZE_1(tenOutgrad); intChannel += 1) {
                        {{type}} fltIn = VALUE_4(tenIn, intN, intChannel, intY, intX);

                        if ((intNorthwestX >= 0) && (intNorthwestX < SIZE_3(tenOutgrad)) && (intNorthwestY >= 0) && (intNorthwestY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intNorthwestY, intNorthwestX) * fltIn * fltNorthwest;
                        }

                        if ((intNortheastX >= 0) && (intNortheastX < SIZE_3(tenOutgrad)) && (intNortheastY >= 0) && (intNortheastY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intNortheastY, intNortheastX) * fltIn * fltNortheast;
                        }

                        if ((intSouthwestX >= 0) && (intSouthwestX < SIZE_3(tenOutgrad)) && (intSouthwestY >= 0) && (intSouthwestY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intSouthwestY, intSouthwestX) * fltIn * fltSouthwest;
                        }

                        if ((intSoutheastX >= 0) && (intSoutheastX < SIZE_3(tenOutgrad)) && (intSoutheastY >= 0) && (intSoutheastY < SIZE_2(tenOutgrad))) {
                            fltFlowgrad += VALUE_4(tenOutgrad, intN, intChannel, intSoutheastY, intSoutheastX) * fltIn * fltSoutheast;
                        }
                    }

                    tenFlowgrad[intIndex] = fltFlowgrad;
                } }
            """,
                    {
                        "tenIn": tenIn,
                        "tenFlow": tenFlow,
                        "tenOutgrad": tenOutgrad,
                        "tenIngrad": tenIngrad,
                        "tenFlowgrad": tenFlowgrad,
                    },
                )
            )(
                grid=tuple([int((tenFlowgrad.nelement() + 512 - 1) / 512), 1, 1]),
                block=tuple([512, 1, 1]),
                args=[
                    cuda_int32(tenFlowgrad.nelement()),
                    tenIn.data_ptr(),
                    tenFlow.data_ptr(),
                    tenOutgrad.data_ptr(),
                    None,
                    tenFlowgrad.data_ptr(),
                ],
                stream=collections.namedtuple("Stream", "ptr")(
                    torch.cuda.current_stream().cuda_stream
                ),
            )
        # end

        return tenIngrad, tenFlowgrad

    # end


# end


def coords_grid(b, h, w, homogeneous=False, device=None):
    y, x = torch.meshgrid(torch.arange(h), torch.arange(w))  # [H, W]

    stacks = [x, y]

    if homogeneous:
        ones = torch.ones_like(x)  # [H, W]
        stacks.append(ones)

    grid = torch.stack(stacks, dim=0).float()  # [2, H, W] or [3, H, W]

    grid = grid[None].repeat(b, 1, 1, 1)  # [B, 2, H, W] or [B, 3, H, W]

    if device is not None:
        grid = grid.to(device)

    return grid


def generate_window_grid(h_min, h_max, w_min, w_max, len_h, len_w, device=None):
    assert device is not None

    x, y = torch.meshgrid(
        [
            torch.linspace(w_min, w_max, len_w, device=device),
            torch.linspace(h_min, h_max, len_h, device=device),
        ],
    )
    grid = torch.stack((x, y), -1).transpose(0, 1).float()  # [H, W, 2]

    return grid


def normalize_coords(coords, h, w):
    # coords: [B, H, W, 2]
    c = torch.Tensor([(w - 1) / 2.0, (h - 1) / 2.0]).float().to(coords.device)
    return (coords - c) / c  # [-1, 1]


def bilinear_sample(
    img, sample_coords, mode="bilinear", padding_mode="zeros", return_mask=False
):
    # img: [B, C, H, W]
    # sample_coords: [B, 2, H, W] in image scale
    if sample_coords.size(1) != 2:  # [B, H, W, 2]
        sample_coords = sample_coords.permute(0, 3, 1, 2)

    b, _, h, w = sample_coords.shape

    # Normalize to [-1, 1]
    x_grid = 2 * sample_coords[:, 0] / (w - 1) - 1
    y_grid = 2 * sample_coords[:, 1] / (h - 1) - 1

    grid = torch.stack([x_grid, y_grid], dim=-1)  # [B, H, W, 2]

    img = F.grid_sample(
        img, grid, mode=mode, padding_mode=padding_mode, align_corners=True
    )

    if return_mask:
        mask = (
            (x_grid >= -1) & (y_grid >= -1) & (x_grid <= 1) & (y_grid <= 1)
        )  # [B, H, W]

        return img, mask

    return img


def flow_warp(feature, flow, mask=False, padding_mode="zeros"):
    b, c, h, w = feature.size()
    assert flow.size(1) == 2

    grid = coords_grid(b, h, w).to(flow.device) + flow  # [B, 2, H, W]

    return bilinear_sample(feature, grid, padding_mode=padding_mode, return_mask=mask)


def forward_backward_consistency_check(fwd_flow, bwd_flow, alpha=0.01, beta=0.5):
    # fwd_flow, bwd_flow: [B, 2, H, W]
    # alpha and beta values are following UnFlow (https://arxiv.org/abs/1711.07837)
    assert fwd_flow.dim() == 4 and bwd_flow.dim() == 4
    assert fwd_flow.size(1) == 2 and bwd_flow.size(1) == 2
    flow_mag = torch.norm(fwd_flow, dim=1) + torch.norm(bwd_flow, dim=1)  # [B, H, W]

    warped_bwd_flow = flow_warp(bwd_flow, fwd_flow)  # [B, 2, H, W]
    warped_fwd_flow = flow_warp(fwd_flow, bwd_flow)  # [B, 2, H, W]

    diff_fwd = torch.norm(fwd_flow + warped_bwd_flow, dim=1)  # [B, H, W]
    diff_bwd = torch.norm(bwd_flow + warped_fwd_flow, dim=1)

    threshold = alpha * flow_mag + beta

    fwd_occ = (diff_fwd > threshold).float()  # [B, H, W]
    bwd_occ = (diff_bwd > threshold).float()

    return fwd_occ, bwd_occ


backwarp_tenGrid = {}


def backwarp(tenIn, tenFlow):
    if str(tenFlow.shape) not in backwarp_tenGrid:
        tenHor = (
            torch.linspace(
                -1.0 + (1.0 / tenFlow.shape[3]),
                1.0 - (1.0 / tenFlow.shape[3]),
                tenFlow.shape[3],
            )
            .view(1, 1, 1, -1)
            .repeat(1, 1, tenFlow.shape[2], 1)
        )
        tenVer = (
            torch.linspace(
                -1.0 + (1.0 / tenFlow.shape[2]),
                1.0 - (1.0 / tenFlow.shape[2]),
                tenFlow.shape[2],
            )
            .view(1, 1, -1, 1)
            .repeat(1, 1, 1, tenFlow.shape[3])
        )

        backwarp_tenGrid[str(tenFlow.shape)] = torch.cat([tenHor, tenVer], 1).cuda()
    # end

    tenFlow = torch.cat(
        [
            tenFlow[:, 0:1, :, :] / ((tenIn.shape[3] - 1.0) / 2.0),
            tenFlow[:, 1:2, :, :] / ((tenIn.shape[2] - 1.0) / 2.0),
        ],
        1,
    )

    return torch.nn.functional.grid_sample(
        input=tenIn,
        grid=(backwarp_tenGrid[str(tenFlow.shape)] + tenFlow).permute(0, 2, 3, 1),
        mode="bilinear",
        padding_mode="zeros",
        align_corners=False,
    )


class MetricNet(nn.Module):
    def __init__(self, partial_fp16):
        super(MetricNet, self).__init__()
        self.metric_net = nn.Sequential(
            nn.Conv2d(14, 64, 3, 1, 1),
            nn.PReLU(),
            nn.Conv2d(64, 64, 3, 1, 1),
            nn.PReLU(),
            nn.Conv2d(64, 2, 3, 1, 1),
        )
        self.partial_fp16 = partial_fp16
        if partial_fp16:
            self.metric_net = self.metric_net.half()

    def forward(self, img0, img1, flow01, flow10):
        metric0 = F.l1_loss(img0, backwarp(img1, flow01), reduction="none").mean(
            [1], True
        )
        metric1 = F.l1_loss(img1, backwarp(img0, flow10), reduction="none").mean(
            [1], True
        )
        fwd_occ, bwd_occ = forward_backward_consistency_check(flow01, flow10)

        if self.partial_fp16:
            img0 = img0.half()
            metric0 = metric0.half()
            flow01 = flow01.half()
            fwd_occ = fwd_occ.half()
            img1 = img1.half()
            metric1 = metric1.half()
            flow10 = flow10.half()
            bwd_occ = bwd_occ.half()

        metric = self.metric_net(
            torch.cat(
                (
                    img0,
                    -metric0,
                    flow01,
                    fwd_occ.unsqueeze(1),
                    img1,
                    -metric1,
                    flow10,
                    bwd_occ.unsqueeze(1),
                ),
                1,
            )
        )

        if self.partial_fp16:
            metric = metric.float()
        return metric[:, :1], metric[:, 1:2]


# Residual Block
def ResidualBlock(in_channels, out_channels, stride=1):
    return torch.nn.Sequential(
        nn.PReLU(),
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=True,
        ),
        nn.PReLU(),
        nn.Conv2d(
            out_channels,
            out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=True,
        ),
    )


# downsample block
def DownsampleBlock(in_channels, out_channels, stride=2):
    return torch.nn.Sequential(
        nn.PReLU(),
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=True,
        ),
        nn.PReLU(),
        nn.Conv2d(
            out_channels, out_channels, kernel_size=3, stride=1, padding=1, bias=True
        ),
    )


# upsample block
def UpsampleBlock(in_channels, out_channels, stride=1):
    return torch.nn.Sequential(
        nn.Upsample(scale_factor=2, mode="bilinear"),
        nn.PReLU(),
        nn.Conv2d(
            in_channels,
            out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=True,
        ),
        nn.PReLU(),
        nn.Conv2d(
            out_channels,
            out_channels,
            kernel_size=3,
            stride=stride,
            padding=1,
            bias=True,
        ),
    )


class PixelShuffleBlcok(nn.Module):
    def __init__(self, in_feat, num_feat, num_out_ch):
        super(PixelShuffleBlcok, self).__init__()
        self.conv_before_upsample = nn.Sequential(
            nn.Conv2d(in_feat, num_feat, 3, 1, 1), nn.PReLU()
        )
        self.upsample = nn.Sequential(
            nn.Conv2d(num_feat, 4 * num_feat, 3, 1, 1), nn.PixelShuffle(2)
        )
        self.conv_last = nn.Conv2d(num_feat, num_out_ch, 3, 1, 1)

    def forward(self, x):
        x = self.conv_before_upsample(x)
        x = self.conv_last(self.upsample(x))
        return x


# grid network
class GridNet(nn.Module):
    def __init__(
        self, in_channels, in_channels1, in_channels2, in_channels3, out_channels
    ):
        super(GridNet, self).__init__()

        self.residual_model_head = ResidualBlock(in_channels, 32, stride=1)
        self.residual_model_head1 = ResidualBlock(in_channels1, 32, stride=1)
        self.residual_model_head4 = ResidualBlock(in_channels // 2, 32, stride=1)
        self.residual_model_head2 = ResidualBlock(in_channels2, 64, stride=1)
        self.residual_model_head3 = ResidualBlock(in_channels3, 96, stride=1)
        self.residual_model_01 = ResidualBlock(32, 32, stride=1)
        # self.residual_model_02=ResidualBlock(32, 32, stride=1)
        # self.residual_model_03=ResidualBlock(32, 32, stride=1)
        self.residual_model_04 = ResidualBlock(32, 32, stride=1)
        self.residual_model_05 = ResidualBlock(32, 32, stride=1)
        self.residual_model_tail = PixelShuffleBlcok(32, 32, out_channels)

        self.residual_model_11 = ResidualBlock(64, 64, stride=1)
        # self.residual_model_12=ResidualBlock(64, 64, stride=1)
        # self.residual_model_13=ResidualBlock(64, 64, stride=1)
        self.residual_model_14 = ResidualBlock(64, 64, stride=1)
        self.residual_model_15 = ResidualBlock(64, 64, stride=1)

        self.residual_model_21 = ResidualBlock(96, 96, stride=1)
        # self.residual_model_22=ResidualBlock(96, 96, stride=1)
        # self.residual_model_23=ResidualBlock(96, 96, stride=1)
        self.residual_model_24 = ResidualBlock(96, 96, stride=1)
        self.residual_model_25 = ResidualBlock(96, 96, stride=1)

        #

        self.downsample_model_10 = DownsampleBlock(32, 64, stride=2)
        self.downsample_model_20 = DownsampleBlock(64, 96, stride=2)

        self.downsample_model_11 = DownsampleBlock(32, 64, stride=2)
        self.downsample_model_21 = DownsampleBlock(64, 96, stride=2)

        # self.downsample_model_12=DownsampleBlock(32, 64, stride=2)
        # self.downsample_model_22=DownsampleBlock(64, 96, stride=2)

        #

        # self.upsample_model_03=UpsampleBlock(64, 32, stride=1)
        # self.upsample_model_13=UpsampleBlock(96, 64, stride=1)

        self.upsample_model_04 = UpsampleBlock(64, 32, stride=1)
        self.upsample_model_14 = UpsampleBlock(96, 64, stride=1)

        self.upsample_model_05 = UpsampleBlock(64, 32, stride=1)
        self.upsample_model_15 = UpsampleBlock(96, 64, stride=1)

    def forward(self, x, x1, x2, x3, x4):
        X00 = (
            self.residual_model_head(x)
            + self.residual_model_head1(x1)
            + self.residual_model_head4(x4)
        )  # ---   182 ~ 185
        # X10 = self.residual_model_head1(x1)

        X01 = self.residual_model_01(X00) + X00  # ---   208 ~ 211 ,AddBackward1213

        X10 = self.downsample_model_10(X00) + self.residual_model_head2(
            x2
        )  # ---   186 ~ 189
        X20 = self.downsample_model_20(X10) + self.residual_model_head3(
            x3
        )  # ---   190 ~ 193

        residual_11 = (
            self.residual_model_11(X10) + X10
        )  # 201 ~ 204    , sum  AddBackward1206
        downsample_11 = self.downsample_model_11(X01)  # 214 ~ 217
        X11 = residual_11 + downsample_11  # ---      AddBackward1218

        residual_21 = (
            self.residual_model_21(X20) + X20
        )  # 194 ~ 197  ,   sum  AddBackward1199
        downsample_21 = self.downsample_model_21(X11)  # 219 ~ 222
        X21 = residual_21 + downsample_21  # AddBackward1223

        X24 = self.residual_model_24(X21) + X21  # ---   224 ~ 227 , AddBackward1229
        X25 = self.residual_model_25(X24) + X24  # ---   230 ~ 233 , AddBackward1235

        upsample_14 = self.upsample_model_14(X24)  # 242 ~ 246
        residual_14 = self.residual_model_14(X11) + X11  # 248 ~ 251, AddBackward1253
        X14 = upsample_14 + residual_14  # ---   AddBackward1254

        upsample_04 = self.upsample_model_04(X14)  # 268 ~ 272
        residual_04 = self.residual_model_04(X01) + X01  # 274 ~ 277, AddBackward1279
        X04 = upsample_04 + residual_04  # ---  AddBackward1280

        upsample_15 = self.upsample_model_15(X25)  # 236 ~ 240
        residual_15 = self.residual_model_15(X14) + X14  # 255 ~ 258, AddBackward1260
        X15 = upsample_15 + residual_15  # AddBackward1261

        upsample_05 = self.upsample_model_05(X15)  # 262 ~ 266
        residual_05 = self.residual_model_05(X04) + X04  # 281 ~ 284,AddBackward1286
        X05 = upsample_05 + residual_05  # AddBackward1287

        X_tail = self.residual_model_tail(X05)  # 288 ~ 291

        return X_tail


class FeatureExtractor(nn.Module):
    """The quadratic model"""

    def __init__(self):
        super(FeatureExtractor, self).__init__()
        self.conv1 = nn.Conv2d(3, 32, 3, stride=2, padding=1)
        self.prelu1 = nn.PReLU()
        self.conv2 = nn.Conv2d(32, 32, 3, padding=1)
        self.prelu2 = nn.PReLU()
        self.conv3 = nn.Conv2d(32, 64, 3, stride=2, padding=1)
        self.prelu3 = nn.PReLU()
        self.conv4 = nn.Conv2d(64, 64, 3, padding=1)
        self.prelu4 = nn.PReLU()
        self.conv5 = nn.Conv2d(64, 96, 3, stride=2, padding=1)
        self.prelu5 = nn.PReLU()
        self.conv6 = nn.Conv2d(96, 96, 3, padding=1)
        self.prelu6 = nn.PReLU()

    def forward(self, x):
        x = self.prelu1(self.conv1(x))
        x1 = self.prelu2(self.conv2(x))
        x = self.prelu3(self.conv3(x1))
        x2 = self.prelu4(self.conv4(x))
        x = self.prelu5(self.conv5(x2))
        x3 = self.prelu6(self.conv6(x))

        return x1, x2, x3


class AnimeInterp(nn.Module):
    """The quadratic model"""

    def __init__(self, partial_fp16=True):
        super(AnimeInterp, self).__init__()
        self.feat_ext = FeatureExtractor()
        self.synnet = GridNet(6, 64, 128, 96 * 2, 3)
        self.partial_fp16 = partial_fp16

        if partial_fp16:
            self.synnet = self.synnet.half()

    def forward(self, I1, I2, reuse_things, merged, t):
        F12, Z1, feat11, feat12, feat13 = (
            reuse_things[0],
            reuse_things[2],
            reuse_things[4][0],
            reuse_things[4][1],
            reuse_things[4][2],
        )
        F21, Z2, feat21, feat22, feat23 = (
            reuse_things[1],
            reuse_things[3],
            reuse_things[5][0],
            reuse_things[5][1],
            reuse_things[5][2],
        )

        F1t = t * F12
        F2t = (1 - t) * F21

        I1t = warp(I1, F1t, Z1, strMode="soft")
        one10 = torch.ones(I1.size(), requires_grad=True).cuda()
        norm1 = warp(one10, F1t, Z1, strMode="soft")
        I1t[norm1 > 0] = I1t[norm1 > 0] / norm1[norm1 > 0]

        I2t = warp(I2, F2t, Z2, strMode="soft")
        one20 = torch.ones(I2.size(), requires_grad=True).cuda()
        norm2 = warp(one20, F2t, Z2, strMode="soft")
        I2t[norm2 > 0] = I2t[norm2 > 0] / norm2[norm2 > 0]

        feat1t1 = warp(feat11, F1t, Z1, strMode="soft")
        one11 = torch.ones(feat11.size(), requires_grad=True).cuda()
        norm1t1 = warp(one11, F1t, Z1, strMode="soft")
        feat1t1[norm1t1 > 0] = feat1t1[norm1t1 > 0] / norm1t1[norm1t1 > 0]

        feat2t1 = warp(feat21, F2t, Z2, strMode="soft")
        one21 = torch.ones(feat21.size(), requires_grad=True).cuda()
        norm2t1 = warp(one21, F2t, Z2, strMode="soft")
        feat2t1[norm2t1 > 0] = feat2t1[norm2t1 > 0] / norm2t1[norm2t1 > 0]

        F1tdd = (
            F.interpolate(F1t, scale_factor=0.5, mode="bilinear", align_corners=False)
            * 0.5
        )
        Z1dd = F.interpolate(Z1, scale_factor=0.5, mode="bilinear", align_corners=False)
        feat1t2 = warp(feat12, F1tdd, Z1dd, strMode="soft")
        one12 = torch.ones(feat12.size(), requires_grad=True).cuda()
        norm1t2 = warp(one12, F1tdd, Z1dd, strMode="soft")
        feat1t2[norm1t2 > 0] = feat1t2[norm1t2 > 0] / norm1t2[norm1t2 > 0]

        F2tdd = (
            F.interpolate(F2t, scale_factor=0.5, mode="bilinear", align_corners=False)
            * 0.5
        )
        Z2dd = F.interpolate(Z2, scale_factor=0.5, mode="bilinear", align_corners=False)
        feat2t2 = warp(feat22, F2tdd, Z2dd, strMode="soft")
        one22 = torch.ones(feat22.size(), requires_grad=True).cuda()
        norm2t2 = warp(one22, F2tdd, Z2dd, strMode="soft")
        feat2t2[norm2t2 > 0] = feat2t2[norm2t2 > 0] / norm2t2[norm2t2 > 0]

        F1tddd = (
            F.interpolate(F1t, scale_factor=0.25, mode="bilinear", align_corners=False)
            * 0.25
        )
        Z1ddd = F.interpolate(
            Z1, scale_factor=0.25, mode="bilinear", align_corners=False
        )
        feat1t3 = warp(feat13, F1tddd, Z1ddd, strMode="soft")
        one13 = torch.ones(feat13.size(), requires_grad=True).cuda()
        norm1t3 = warp(one13, F1tddd, Z1ddd, strMode="soft")
        feat1t3[norm1t3 > 0] = feat1t3[norm1t3 > 0] / norm1t3[norm1t3 > 0]

        F2tddd = (
            F.interpolate(F2t, scale_factor=0.25, mode="bilinear", align_corners=False)
            * 0.25
        )
        Z2ddd = F.interpolate(
            Z2, scale_factor=0.25, mode="bilinear", align_corners=False
        )
        feat2t3 = warp(feat23, F2tddd, Z2ddd, strMode="soft")
        one23 = torch.ones(feat23.size(), requires_grad=True).cuda()
        norm2t3 = warp(one23, F2tddd, Z2ddd, strMode="soft")
        feat2t3[norm2t3 > 0] = feat2t3[norm2t3 > 0] / norm2t3[norm2t3 > 0]

        if self.partial_fp16:
            It_warp = self.synnet(
                torch.cat([I1t, I2t], dim=1).half(),
                torch.cat([feat1t1, feat2t1], dim=1).half(),
                torch.cat([feat1t2, feat2t2], dim=1).half(),
                torch.cat([feat1t3, feat2t3], dim=1).half(),
                merged.half(),
            )
            It_warp = It_warp.float()

        if not self.partial_fp16:
            It_warp = self.synnet(
                torch.cat([I1t, I2t], dim=1),
                torch.cat([feat1t1, feat2t1], dim=1),
                torch.cat([feat1t2, feat2t2], dim=1),
                torch.cat([feat1t3, feat2t3], dim=1),
                merged,
            )

        return torch.clamp(It_warp, 0, 1)


from .rife_arch import IFNet


class Model:
    def __init__(self, partial_fp16):
        self.flownet = GMFlow(partial_fp16=partial_fp16)
        self.ifnet = IFNet(arch_ver="4.6")
        self.metricnet = MetricNet(partial_fp16=partial_fp16)
        self.fusionnet = AnimeInterp(partial_fp16=partial_fp16)
        self.version = 3.9
        self.model_type = "vanilla"
        self.partial_fp16 = partial_fp16

    def eval(self):
        self.flownet.eval()
        self.ifnet.eval()
        self.metricnet.eval()
        self.fusionnet.eval()

    def device(self):
        self.flownet.to(device)
        self.ifnet.to(device)
        self.metricnet.to(device)
        self.fusionnet.to(device)

    def load_model(self, path):
        if self.model_type == "vanilla":
            metricnet_path = "{}/GMFSS_union_metric_vanilla.pkl".format(path)
            check_and_download(metricnet_path)
            self.metricnet.load_state_dict(torch.load(metricnet_path))

            fusionnet_path = "{}/GMFSS_union_fusionnet_vanilla.pkl".format(path)
            check_and_download(fusionnet_path)
            self.fusionnet.load_state_dict(torch.load(fusionnet_path))

        flownet_path = "{}/GMFupSS_flownet.pkl".format(path)
        check_and_download(flownet_path)
        self.flownet.load_state_dict(torch.load(flownet_path))

        ifnet_path = "/workspace/tensorrt/models/rife46.pth"
        check_and_download(ifnet_path)
        self.ifnet.load_state_dict(torch.load(ifnet_path))
        if self.partial_fp16:
            self.ifnet.half()

    def reuse(self, img0, img1, scale):
        feat11, feat12, feat13 = self.fusionnet.feat_ext((img0 - 0.5) / 0.5)
        feat21, feat22, feat23 = self.fusionnet.feat_ext((img1 - 0.5) / 0.5)
        feat_ext0 = [feat11, feat12, feat13]
        feat_ext1 = [feat21, feat22, feat23]

        img0 = F.interpolate(
            img0, scale_factor=0.5, mode="bilinear", align_corners=False
        )
        img1 = F.interpolate(
            img1, scale_factor=0.5, mode="bilinear", align_corners=False
        )

        if scale != 1.0:
            imgf0 = F.interpolate(
                img0, scale_factor=scale, mode="bilinear", align_corners=False
            )
            imgf1 = F.interpolate(
                img1, scale_factor=scale, mode="bilinear", align_corners=False
            )
        else:
            imgf0 = img0
            imgf1 = img1
        flow01 = self.flownet(imgf0, imgf1)
        flow10 = self.flownet(imgf1, imgf0)
        if scale != 1.0:
            flow01 = (
                F.interpolate(
                    flow01,
                    scale_factor=1.0 / scale,
                    mode="bilinear",
                    align_corners=False,
                )
                / scale
            )
            flow10 = (
                F.interpolate(
                    flow10,
                    scale_factor=1.0 / scale,
                    mode="bilinear",
                    align_corners=False,
                )
                / scale
            )

        metric0, metric1 = self.metricnet(img0, img1, flow01, flow10)

        return flow01, flow10, metric0, metric1, feat_ext0, feat_ext1

    def inference(
        self, I0, I1, flow01, flow10, metric0, metric1, feat_ext0, feat_ext1, timestep
    ):
        I0 = F.interpolate(I0, scale_factor=0.5, mode="bilinear", align_corners=False)
        I1 = F.interpolate(I1, scale_factor=0.5, mode="bilinear", align_corners=False)

        scale_list = [8, 4, 2, 1]
        if self.partial_fp16:
            I0 = I0.half()
            I1 = I1.half()
        with torch.inference_mode():
            merged = self.ifnet(I0, I1, timestep, scale_list)
        if self.partial_fp16:
            I0 = I0.float()
            I1 = I1.float()
        out = self.fusionnet(
            I0,
            I1,
            (flow01, flow10, metric0, metric1, feat_ext0, feat_ext1),
            merged,
            timestep,
        )
        return out


class Model_inference(nn.Module):
    def __init__(self, partial_fp16=True):
        super(Model_inference, self).__init__()
        self.model = Model(partial_fp16)
        self.model.eval()
        self.model.device()
        self.model.load_model("/workspace/tensorrt/models/")

    def forward(self, I0, I1, timestep, scale=1.0):
        # padding frames
        n, c, h, w = I0.shape
        # print(n, c, h, w)
        tmp = max(64, int(64 / scale))
        ph = ((h - 1) // tmp + 1) * tmp
        pw = ((w - 1) // tmp + 1) * tmp
        padding = (0, pw - w, 0, ph - h)
        I0 = F.pad(I0, padding)
        I1 = F.pad(I1, padding)
        flow01, flow10, metric0, metric1, feat_ext0, feat_ext1 = self.model.reuse(
            I0, I1, scale=1.0
        )
        output = self.model.inference(
            I0, I1, flow01, flow10, metric0, metric1, feat_ext0, feat_ext1, timestep
        )
        return output[0][:, :h, :w]
