from contextlib import contextmanager

import torch
from torch import nn
from torch.nn import functional as F
from torch.cuda import nvtx
from torch.utils.data import BatchSampler, Subset, DataLoader

_REQUIRES_GRAD_ATTR = '_original_requires_grad'

__all__ = [
    'original_requires_grad', 'record_original_requires_grad',
    'restore_original_requires_grad', 'disable_param_grad', 'im2col_2d',
    'im2col_2d_slow', 'add_value_to_diagonal', 'nvtx_range', 'cholesky_inv',
    'PseudoBatchLoaderGenerator'
]


def original_requires_grad(module, param_name):
    param = getattr(module, param_name, None)
    return param is not None and getattr(param, _REQUIRES_GRAD_ATTR)


def record_original_requires_grad(param):
    setattr(param, _REQUIRES_GRAD_ATTR, param.requires_grad)


def restore_original_requires_grad(param):
    param.requires_grad = getattr(param, _REQUIRES_GRAD_ATTR,
                                  param.requires_grad)


@contextmanager
def disable_param_grad(model, disable=True):
    if disable:
        for param in model.parameters():
            record_original_requires_grad(param)
            param.requires_grad = False

    yield
    if disable:
        for param in model.parameters():
            restore_original_requires_grad(param)


def im2col_2d(x: torch.Tensor, conv2d: nn.Module):
    assert x.ndimension() == 4  # n x c x h_in x w_in
    assert isinstance(conv2d, (nn.Conv2d, nn.ConvTranspose2d))
    assert conv2d.dilation == (1, 1)

    ph, pw = conv2d.padding
    kh, kw = conv2d.kernel_size
    sy, sx = conv2d.stride
    if ph + pw > 0:
        x = F.pad(x, (pw, pw, ph, ph)).data
    x = x.unfold(2, kh, sy)  # n x c x h_out x w_in x kh
    x = x.unfold(3, kw, sx)  # n x c x h_out x w_out x kh x kw
    x = x.permute(0, 1, 4, 5, 2,
                  3).contiguous()  # n x c x kh x kw x h_out x w_out
    x = x.view(x.size(0),
               x.size(1) * x.size(2) * x.size(3),
               x.size(4) * x.size(5))  # n x c(kh)(kw) x (h_out)(w_out)
    return x


def im2col_2d_slow(x: torch.Tensor, conv2d: nn.Module):
    assert x.ndimension() == 4  # n x c x h_in x w_in
    assert isinstance(conv2d, (nn.Conv2d, nn.ConvTranspose2d))

    # n x c(k_h)(k_w) x (h_out)(w_out)
    Mx = F.unfold(x,
                  conv2d.kernel_size,
                  dilation=conv2d.dilation,
                  padding=conv2d.padding,
                  stride=conv2d.stride)

    return Mx


def add_value_to_diagonal(x: torch.Tensor, value):
    ndim = x.ndim
    assert ndim >= 2
    eye = torch.eye(x.shape[-1], device=x.device)
    if ndim > 2:
        shape = tuple(x.shape[:-2]) + (1, 1)
        eye = eye.repeat(*shape)
    return x.add(eye, alpha=value)


@contextmanager
def nvtx_range(msg):
    try:
        nvtx.range_push(msg)
        yield
    finally:
        nvtx.range_pop()


def cholesky_inv(X):
    u = torch.linalg.cholesky(X)
    return torch.cholesky_inverse(u)


class PseudoBatchLoaderGenerator:
    """
    Example::
    >>> # create a base dataloader
    >>> dataset_size = 10
    >>> x_all = torch.tensor(range(dataset_size))
    >>> dataset = torch.utils.data.TensorDataset(x_all)
    >>> data_loader = torch.utils.data.DataLoader(dataset, shuffle=True)
    >>>
    >>> # create a pseudo-batch loader generator
    >>> pb_loader_generator = PseudoBatchLoaderGenerator(data_loader, 5)
    >>>
    >>> for i, pb_loader in enumerate(pb_loader_generator):
    >>>     print(f'pseudo-batch at step {i}')
    >>>     print(list(pb_loader))

    Outputs:
    ```
    pseudo-batch at step 0
    [[tensor([0])], [tensor([1])], [tensor([3])], [tensor([6])], [tensor([7])]]
    pseudo-batch at step 1
    [[tensor([8])], [tensor([5])], [tensor([4])], [tensor([2])], [tensor([9])]]
    ```
    """
    def __init__(self, base_data_loader, pseudo_batch_size, batch_size=None):
        if batch_size is None:
            batch_size = base_data_loader.batch_size
        assert pseudo_batch_size % batch_size == 0, f'pseudo_batch_size ({pseudo_batch_size}) ' \
                                                    f'needs to be divisible by batch_size ({batch_size})'
        base_dataset = base_data_loader.dataset
        sampler_cls = base_data_loader.sampler.__class__
        pseudo_batch_sampler = BatchSampler(sampler_cls(range(len(base_dataset))),
                                            batch_size=pseudo_batch_size,
                                            drop_last=True)
        self.batch_size = batch_size
        self.pseudo_batch_sampler = pseudo_batch_sampler
        self.base_dataset = base_dataset
        self.base_data_loader = base_data_loader

    def __iter__(self):
        loader = self.base_data_loader
        for indices in self.pseudo_batch_sampler:
            subset_in_pseudo_batch = Subset(self.base_dataset, indices)
            data_loader = DataLoader(
                subset_in_pseudo_batch,
                batch_size=self.batch_size,
                shuffle=False,
                num_workers=loader.num_workers,
                collate_fn=loader.collate_fn,
                pin_memory=loader.pin_memory,
                drop_last=False,
                timeout=loader.timeout,
                worker_init_fn=loader.worker_init_fn,
                multiprocessing_context=loader.multiprocessing_context,
                generator=loader.generator,
                prefetch_factor=loader.prefetch_factor,
                persistent_workers=loader.persistent_workers)
            yield data_loader
