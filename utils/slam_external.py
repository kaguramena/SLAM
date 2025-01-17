"""
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use
# under the terms of the LICENSE.md file found here:
# https://github.com/graphdeco-inria/gaussian-splatting/blob/main/LICENSE.md
#
# For inquiries contact  george.drettakis@inria.fr

#######################################################################################################################
##### NOTE: CODE IN THIS FILE IS NOT INCLUDED IN THE OVERALL PROJECT'S MIT LICENSE #####
##### USE OF THIS CODE FOLLOWS THE COPYRIGHT NOTICE ABOVE #####
#######################################################################################################################
"""

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as func
from torch.autograd import Variable
from math import exp


def build_rotation(q):
    norm = torch.sqrt(q[:, 0] * q[:, 0] + q[:, 1] * q[:, 1] + q[:, 2] * q[:, 2] + q[:, 3] * q[:, 3])
    q = q / norm[:, None]
    rot = torch.zeros((q.size(0), 3, 3), device='cuda')
    r = q[:, 0]
    x = q[:, 1]
    y = q[:, 2]
    z = q[:, 3]
    rot[:, 0, 0] = 1 - 2 * (y * y + z * z)
    rot[:, 0, 1] = 2 * (x * y - r * z)
    rot[:, 0, 2] = 2 * (x * z + r * y)
    rot[:, 1, 0] = 2 * (x * y + r * z)
    rot[:, 1, 1] = 1 - 2 * (x * x + z * z)
    rot[:, 1, 2] = 2 * (y * z - r * x)
    rot[:, 2, 0] = 2 * (x * z - r * y)
    rot[:, 2, 1] = 2 * (y * z + r * x)
    rot[:, 2, 2] = 1 - 2 * (x * x + y * y)
    return rot


def calc_mse(img1, img2):
    return ((img1 - img2) ** 2).view(img1.shape[0], -1).mean(1, keepdim=True)


def calc_psnr(img1, img2):
    mse = ((img1 - img2) ** 2).view(img1.shape[0], -1).mean(1, keepdim=True)
    return 20 * torch.log10(1.0 / torch.sqrt(mse))


def gaussian(window_size, sigma):
    gauss = torch.Tensor([exp(-(x - window_size // 2) ** 2 / float(2 * sigma ** 2)) for x in range(window_size)])
    return gauss / gauss.sum()


def create_window(window_size, channel):
    _1D_window = gaussian(window_size, 1.5).unsqueeze(1)
    _2D_window = _1D_window.mm(_1D_window.t()).float().unsqueeze(0).unsqueeze(0)
    window = Variable(_2D_window.expand(channel, 1, window_size, window_size).contiguous())
    return window


def calc_ssim(img1, img2, window_size=11, size_average=True):
    channel = img1.size(-3)
    window = create_window(window_size, channel)

    if img1.is_cuda:
        window = window.cuda(img1.get_device())
    window = window.type_as(img1)

    return _ssim(img1, img2, window, window_size, channel, size_average)


def _ssim(img1, img2, window, window_size, channel, size_average=True):
    mu1 = func.conv2d(img1, window, padding=window_size // 2, groups=channel)
    mu2 = func.conv2d(img2, window, padding=window_size // 2, groups=channel)

    mu1_sq = mu1.pow(2)
    mu2_sq = mu2.pow(2)
    mu1_mu2 = mu1 * mu2

    sigma1_sq = func.conv2d(img1 * img1, window, padding=window_size // 2, groups=channel) - mu1_sq
    sigma2_sq = func.conv2d(img2 * img2, window, padding=window_size // 2, groups=channel) - mu2_sq
    sigma12 = func.conv2d(img1 * img2, window, padding=window_size // 2, groups=channel) - mu1_mu2

    c1 = 0.01 ** 2
    c2 = 0.03 ** 2

    ssim_map = ((2 * mu1_mu2 + c1) * (2 * sigma12 + c2)) / ((mu1_sq + mu2_sq + c1) * (sigma1_sq + sigma2_sq + c2))

    if size_average:
        return ssim_map.mean()
    else:
        return ssim_map.mean(1).mean(1).mean(1)


def accumulate_mean2d_gradient(variables):
    variables['means2D_gradient_accum'][variables['seen']] += torch.norm(
        variables['means2D'].grad[variables['seen'], :2], dim=-1)
    variables['denom'][variables['seen']] += 1
    return variables


def update_params_and_optimizer(new_params, params, optimizer):
    for k, v in new_params.items():
        group = [x for x in optimizer.param_groups if x["name"] == k][0]
        stored_state = optimizer.state.get(group['params'][0], None)

        stored_state["exp_avg"] = torch.zeros_like(v)
        stored_state["exp_avg_sq"] = torch.zeros_like(v)
        del optimizer.state[group['params'][0]]

        group["params"][0] = torch.nn.Parameter(v.requires_grad_(True))
        optimizer.state[group['params'][0]] = stored_state
        params[k] = group["params"][0]
    return params


def cat_params_to_optimizer(new_params, params, optimizer):
    for k, v in new_params.items():
        group = [g for g in optimizer.param_groups if g['name'] == k][0]
        stored_state = optimizer.state.get(group['params'][0], None)
        if stored_state is not None:
            stored_state["exp_avg"] = torch.cat((stored_state["exp_avg"], torch.zeros_like(v)), dim=0)
            stored_state["exp_avg_sq"] = torch.cat((stored_state["exp_avg_sq"], torch.zeros_like(v)), dim=0)
            del optimizer.state[group['params'][0]]
            group["params"][0] = torch.nn.Parameter(torch.cat((group["params"][0], v), dim=0).requires_grad_(True))
            optimizer.state[group['params'][0]] = stored_state
            params[k] = group["params"][0]
        else:
            group["params"][0] = torch.nn.Parameter(torch.cat((group["params"][0], v), dim=0).requires_grad_(True))
            params[k] = group["params"][0]
    return params


def remove_points(to_remove, params, variables, optimizer):
    to_keep = ~to_remove
    assert(to_keep.shape[0] == params['means3D'].shape[0])
    keys = [k for k in params.keys() if k not in ['cam_unnorm_rots', 'cam_trans']]
    for k in keys:
        group = [g for g in optimizer.param_groups if g['name'] == k][0]
        stored_state = optimizer.state.get(group['params'][0], None)
        if stored_state is not None:
            stored_state["exp_avg"] = stored_state["exp_avg"][to_keep]
            stored_state["exp_avg_sq"] = stored_state["exp_avg_sq"][to_keep]
            del optimizer.state[group['params'][0]]
            group["params"][0] = torch.nn.Parameter((group["params"][0][to_keep].requires_grad_(True)))
            optimizer.state[group['params'][0]] = stored_state
            params[k] = group["params"][0]
        else:
            group["params"][0] = torch.nn.Parameter(group["params"][0][to_keep].requires_grad_(True))
            params[k] = group["params"][0]
    variables['means2D_gradient_accum'] = variables['means2D_gradient_accum'][to_keep]
    variables['seen'] = variables['seen'][to_keep]
    variables['denom'] = variables['denom'][to_keep]
    variables['max_2D_radius'] = variables['max_2D_radius'][to_keep]
    variables['camera_means3D'] = variables['camera_means3D'][to_keep]
    original_grad = variables['means2D'].grad.clone()
    # 执行筛选
    variables['means2D'] = variables['means2D'][to_keep]
    # 重新附加梯度
    if original_grad is not None:
        variables['means2D'].grad = original_grad[to_keep]
    if 'timestep' in variables.keys():
        variables['timestep'] = variables['timestep'][to_keep]
    return params, variables


def inverse_sigmoid(x):
    return torch.log(x / (1 - x))


def map_screen_to_gaussians(means3D, image_width, image_height):
    """
    使用 Tensor 操作计算高斯在屏幕上的映射，确保正确性。
    
    Args:
        means3D: 高斯中心在相机坐标系中的 3D 坐标 [num_gaussians, 3]
        image_width: 图像宽度
        image_height: 图像高度
        
    Returns:
        screen_to_gaussians: Tensor，形状为 [image_height * image_width]，
                             其中每个元素表示屏幕像素索引对应的高斯索引。
    """
    # 提取 means3D 的前两个分量，映射到屏幕坐标
    means2D = means3D[:, :2]  # [num_gaussians, 2]
    num_gaussians = means2D.shape[0]
    
    # 将 means2D 映射到实际屏幕坐标
    screen_coords = torch.stack([
        (means2D[:, 0] * (image_width - 1)).long().clamp(0, image_width - 1),
        (means2D[:, 1] * (image_height - 1)).long().clamp(0, image_height - 1)
    ], dim=-1)  # [num_gaussians, 2]

    # 计算线性索引：y * width + x
    screen_linear_indices = screen_coords[:, 1] * image_width + screen_coords[:, 0]  # [num_gaussians]
    
    # 初始化结果 Tensor，使用 -1 作为默认值
    screen_to_gaussians = torch.full((image_width * image_height,), -1, dtype=torch.long, device=means3D.device)
    
    # 使用 scatter_ 来批量将高斯索引映射到对应的屏幕像素，优先保留第一个高斯
    gaussian_indices = torch.arange(num_gaussians, device=means3D.device)
    screen_to_gaussians.scatter_(0, screen_linear_indices, gaussian_indices)
    assert(screen_to_gaussians.max() < num_gaussians , "Max less than num_GS")
    assert(screen_to_gaussians.min() > 0, " Min bigger than 0")
    
    return screen_to_gaussians

def prune_gaussians(params, variables, optimizer, iter, prune_dict,curr_data):
    if iter <= prune_dict['stop_after']:
        if (iter >= prune_dict['start_after']):
            if iter == prune_dict['stop_after']:
                remove_threshold = prune_dict['final_removal_opacity_threshold']
            else:
                remove_threshold = prune_dict['removal_opacity_threshold']
            # Remove Gaussians with low opacity
            to_remove = (torch.sigmoid(params['logit_opacities']) < remove_threshold).squeeze()
            # Remove Gaussians that are too big
            if iter >= prune_dict['remove_big_after']:
                big_points_ws = torch.exp(params['log_scales']).max(dim=1).values > 0.1 * variables['scene_radius']
                to_remove = torch.logical_or(to_remove, big_points_ws)

            pixel_to_gs = map_screen_to_gaussians(variables['camera_means3D'],curr_data['depth'].shape[1],curr_data['depth'].shape[2])
            depth_sil = variables['depth_sil']
            depth = depth_sil[0, :, :].unsqueeze(0)
            silhouette = depth_sil[1, :, :]
            depth_error = abs(depth - curr_data['depth'])
            depth_remove_mask = torch.zeros_like(depth,dtype=torch.bool)
            depth_remove_mask = (silhouette > 0.99) & (abs(depth - curr_data['depth']) > ((depth_error.max() - 0.01)))# To pixel
            depth_remove_mask = depth_remove_mask.reshape(-1) # To pixel
            
            # and now should pick gs from pixel
            gaussian_mask = pixel_to_gs[depth_remove_mask]
            gaussian_mask = gaussian_mask[gaussian_mask >= 0]
            
            # num_gs = params['means3D'].shape[0]
            # print(f'max_gs : {gaussian_mask.max()} ,min_gs :{gaussian_mask.min()}, num : {num_gs}')

            depth_to_remove = torch.zeros_like(to_remove)
            depth_to_remove[gaussian_mask] = 1

            assert(depth_to_remove.shape[0] == to_remove.shape[0])
            # num_gs = params['means3D'].shape[0]
            # print(f'max_gs : {gaussian_mask.max()} ,min_gs :{gaussian_mask.min()}, num : {num_gs}, is_nan : {torch.isnan(depth_to_remove).any()}')

            
            # 将有效索引对应的位置设置为 True
            to_remove = torch.logical_or(to_remove,depth_to_remove)
            # print(depth_to_remove.sum(),to_remove.sum())
            # 移除高斯点
            params, variables = remove_points(to_remove, params, variables, optimizer)
            torch.cuda.empty_cache()
        
        # Reset Opacities for all Gaussians
        if iter > 0 and iter % prune_dict['reset_opacities_every'] == 0 and prune_dict['reset_opacities']:
            new_params = {'logit_opacities': inverse_sigmoid(torch.ones_like(params['logit_opacities']) * 0.01)}
            params = update_params_and_optimizer(new_params, params, optimizer)
    
    return params, variables


def densify(params, variables, optimizer, iter, densify_dict):
    if iter <= densify_dict['stop_after']:
        variables = accumulate_mean2d_gradient(variables)
        grad_thresh = densify_dict['grad_thresh']
        if (iter >= densify_dict['start_after']) and (iter % densify_dict['densify_every'] == 0):
            grads = variables['means2D_gradient_accum'] / variables['denom']
            grads[grads.isnan()] = 0.0
            to_clone = torch.logical_and(grads >= grad_thresh, (
                        torch.max(torch.exp(params['log_scales']), dim=1).values <= 0.01 * variables['scene_radius']))
            new_params = {k: v[to_clone] for k, v in params.items() if k not in ['cam_unnorm_rots', 'cam_trans']}
            params = cat_params_to_optimizer(new_params, params, optimizer)
            num_pts = params['means3D'].shape[0]

            padded_grad = torch.zeros(num_pts, device="cuda")
            padded_grad[:grads.shape[0]] = grads
            to_split = torch.logical_and(padded_grad >= grad_thresh,
                                         torch.max(torch.exp(params['log_scales']), dim=1).values > 0.01 * variables[
                                             'scene_radius'])
            n = densify_dict['num_to_split_into']  # number to split into
            new_params = {k: v[to_split].repeat(n, 1) for k, v in params.items() if k not in ['cam_unnorm_rots', 'cam_trans']}
            stds = torch.exp(params['log_scales'])[to_split].repeat(n, 3)
            means = torch.zeros((stds.size(0), 3), device="cuda")
            samples = torch.normal(mean=means, std=stds)
            rots = build_rotation(params['unnorm_rotations'][to_split]).repeat(n, 1, 1)
            new_params['means3D'] += torch.bmm(rots, samples.unsqueeze(-1)).squeeze(-1)
            new_params['log_scales'] = torch.log(torch.exp(new_params['log_scales']) / (0.8 * n))
            params = cat_params_to_optimizer(new_params, params, optimizer)
            num_pts = params['means3D'].shape[0]

            variables['means2D_gradient_accum'] = torch.zeros(num_pts, device="cuda")
            variables['denom'] = torch.zeros(num_pts, device="cuda")
            variables['max_2D_radius'] = torch.zeros(num_pts, device="cuda")
            to_remove = torch.cat((to_split, torch.zeros(n * to_split.sum(), dtype=torch.bool, device="cuda")))
            params, variables = remove_points(to_remove, params, variables, optimizer)

            if iter == densify_dict['stop_after']:
                remove_threshold = densify_dict['final_removal_opacity_threshold']
            else:
                remove_threshold = densify_dict['removal_opacity_threshold']
            to_remove = (torch.sigmoid(params['logit_opacities']) < remove_threshold).squeeze()
            if iter >= densify_dict['remove_big_after']:
                big_points_ws = torch.exp(params['log_scales']).max(dim=1).values > 0.1 * variables['scene_radius']
                to_remove = torch.logical_or(to_remove, big_points_ws)
            params, variables = remove_points(to_remove, params, variables, optimizer)

            torch.cuda.empty_cache()

        # Reset Opacities for all Gaussians (This is not desired for mapping on only current frame)
        if iter > 0 and iter % densify_dict['reset_opacities_every'] == 0 and densify_dict['reset_opacities']:
            new_params = {'logit_opacities': inverse_sigmoid(torch.ones_like(params['logit_opacities']) * 0.01)}
            params = update_params_and_optimizer(new_params, params, optimizer)

    return params, variables


def update_learning_rate(optimizer, means3D_scheduler, iteration):
        ''' Learning rate scheduling per step '''
        for param_group in optimizer.param_groups:
            if param_group["name"] == "means3D":
                lr = means3D_scheduler(iteration)
                param_group['lr'] = lr
                return lr


def get_expon_lr_func(
    lr_init, lr_final, lr_delay_steps=0, lr_delay_mult=1.0, max_steps=1000000
):
    """
    Copied from Plenoxels

    Continuous learning rate decay function. Adapted from JaxNeRF
    The returned rate is lr_init when step=0 and lr_final when step=max_steps, and
    is log-linearly interpolated elsewhere (equivalent to exponential decay).
    If lr_delay_steps>0 then the learning rate will be scaled by some smooth
    function of lr_delay_mult, such that the initial learning rate is
    lr_init*lr_delay_mult at the beginning of optimization but will be eased back
    to the normal learning rate when steps>lr_delay_steps.
    :param conf: config subtree 'lr' or similar
    :param max_steps: int, the number of steps during optimization.
    :return HoF which takes step as input
    """

    def helper(step):
        if step < 0 or (lr_init == 0.0 and lr_final == 0.0):
            # Disable this parameter
            return 0.0
        if lr_delay_steps > 0:
            # A kind of reverse cosine decay.
            delay_rate = lr_delay_mult + (1 - lr_delay_mult) * np.sin(
                0.5 * np.pi * np.clip(step / lr_delay_steps, 0, 1)
            )
        else:
            delay_rate = 1.0
        t = np.clip(step / max_steps, 0, 1)
        log_lerp = np.exp(np.log(lr_init) * (1 - t) + np.log(lr_final) * t)
        return delay_rate * log_lerp

    return helper


# 定义拉普拉斯算子卷积核
laplacian_kernel = torch.tensor([[[[0, 1, 0], [1, -4, 1], [0, 1, 0]]]], dtype=torch.float32)

# 定义拉普拉斯操作函数
def laplacian_operator(image):
    device = image.device  # 获取输入图像的设备（CPU或CUDA）
    
    laplacian_conv = nn.Conv2d(1, 1, kernel_size=3, padding=1, bias=False).to(device)
    laplacian_kernel_device = laplacian_kernel.to(device)
    laplacian_conv.weight = nn.Parameter(laplacian_kernel_device)

    # 如果图像是 RGB (3 通道)，则将其转换为灰度图像
    if image.shape[1] == 3:  # 检查通道数
        image = torch.mean(image, dim=1, keepdim=True)  # 转换为灰度图像
    
    return laplacian_conv(image)


import torch
import numpy as np
import torch.nn.functional as F

def optical_flow_loss(img1: torch.Tensor, img2: torch.Tensor) -> torch.Tensor:
    """
    计算两张图像之间的光流损失，损失为 sqrt(u^2 + v^2)，
    其中 u 和 v 是光流在水平和垂直方向上的分量。

    参数:
    - img1 (torch.Tensor): 第一张图像，形状为 (C, H, W)。
    - img2 (torch.Tensor): 第二张图像，形状为 (C, H, W)。

    返回:
    - torch.Tensor: 计算得到的光流损失。
    """
    # 如果图像是 RGB (3 通道)，则将其转换为灰度图像
    if img1.shape[0] == 3:  # RGB -> 灰度
        img1 = img1.mean(dim=0, keepdim=True)
        img2 = img2.mean(dim=0, keepdim=True)

    # 计算图像的梯度（使用Sobel算子）
    sobel_kernel_x = torch.tensor([[[[-1, 0, 1], [-2, 0, 2], [-1, 0, 1]]]], dtype=torch.float32).to(img1.device)
    sobel_kernel_y = torch.tensor([[[[-1, -2, -1], [0, 0, 0], [1, 2, 1]]]], dtype=torch.float32).to(img1.device)

    img1_dx = F.conv2d(img1.unsqueeze(0), weight=sobel_kernel_x, padding=1)
    img1_dy = F.conv2d(img1.unsqueeze(0), weight=sobel_kernel_y, padding=1)
    img2_dx = F.conv2d(img2.unsqueeze(0), weight=sobel_kernel_x, padding=1)
    img2_dy = F.conv2d(img2.unsqueeze(0), weight=sobel_kernel_y, padding=1)

    # 计算光流的水平和垂直分量
    u = img2_dx - img1_dx
    v = img2_dy - img1_dy

    # 计算光流损失
    flow_loss = torch.sqrt(u**2 + v**2).mean()  # 计算每个像素点的光流模长，并取平均

    return flow_loss


