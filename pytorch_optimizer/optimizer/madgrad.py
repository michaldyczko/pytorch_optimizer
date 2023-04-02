# Copyright (c) Facebook, Inc. and its affiliates.
#
# This source code is licensed under the MIT license found in the
# LICENSE file in the root directory of this source tree.

import math

import torch
from torch.optim import Optimizer

from pytorch_optimizer.base.exception import NoSparseGradientError
from pytorch_optimizer.base.optimizer import BaseOptimizer
from pytorch_optimizer.base.types import CLOSURE, DEFAULTS, LOSS, PARAMETERS


class MADGRAD(Optimizer, BaseOptimizer):
    r"""A Momentumized, Adaptive, Dual Averaged Gradient Method for Stochastic (slightly modified).

    :param params: PARAMETERS. iterable of parameters to optimize or dicts defining parameter groups.
    :param lr: float. learning rate.
    :param eps: float. term added to the denominator to improve numerical stability.
    :param weight_decay: float. weight decay (L2 penalty).
        MADGRAD optimizer requires less weight decay than other methods, often as little as zero.
        On sparse problems both weight_decay and momentum should be set to 0.
    :param decouple_decay: float. Apply AdamW style decoupled weight decay.
    """

    def __init__(
        self,
        params: PARAMETERS,
        lr: float = 1e-3,
        momentum: float = 0.9,
        weight_decay: float = 0.0,
        decouple_decay: bool = False,
        eps: float = 1e-6,
    ):
        self.lr = lr
        self.momentum = momentum
        self.weight_decay = weight_decay
        self.decouple_decay = decouple_decay
        self.eps = eps

        self.validate_parameters()

        defaults: DEFAULTS = {'lr': lr, 'weight_decay': weight_decay, 'momentum': momentum, 'eps': eps}
        super().__init__(params, defaults)

    def validate_parameters(self):
        self.validate_learning_rate(self.lr)
        self.validate_weight_decay(self.weight_decay)
        self.validate_momentum(self.momentum)
        self.validate_epsilon(self.eps)

    def __str__(self) -> str:
        return 'MADGRAD'

    @torch.no_grad()
    def reset(self):
        self.state['k'] = torch.tensor([0], dtype=torch.long, requires_grad=False)

        for group in self.param_groups:
            for p in group['params']:
                state = self.state[p]

                state['grad_sum_sq'] = torch.zeros_like(p)
                state['s'] = torch.zeros_like(p)
                if group['momentum'] > 0.0:
                    state['x0'] = torch.clone(p).detach()

    @torch.no_grad()
    def step(self, closure: CLOSURE = None) -> LOSS:
        loss: LOSS = None
        if closure is not None:
            with torch.enable_grad():
                loss = closure()

        # step counter must be stored in state to ensure correct behavior under optimizer sharding
        if 'k' not in self.state:
            self.state['k'] = torch.tensor([0], dtype=torch.long, requires_grad=False)

        for group in self.param_groups:
            weight_decay, momentum, eps = group['weight_decay'], group['momentum'], group['eps']
            lr = group['lr'] + eps

            _lambda = lr * math.pow(self.state['k'] + 1, 0.5)

            for p in group['params']:
                if p.grad is None:
                    continue

                grad = p.grad
                state = self.state[p]

                if 'grad_sum_sq' not in state:
                    state['grad_sum_sq'] = torch.zeros_like(p)
                    state['s'] = torch.zeros_like(p)
                    if momentum > 0.0:
                        state['x0'] = torch.clone(p).detach()

                if momentum > 0.0 and grad.is_sparse:
                    raise NoSparseGradientError(str(self), note='momentum > 0.0')

                grad_sum_sq, s = state['grad_sum_sq'], state['s']

                if weight_decay > 0.0 and not self.decouple_decay:
                    if grad.is_sparse:
                        raise NoSparseGradientError(str(self), note='weight_decay')

                    # original implementation. not AdamW style
                    grad.add_(p, alpha=weight_decay)

                if grad.is_sparse:
                    grad = grad.coalesce()

                    p_masked = p.sparse_mask(grad)
                    grad_sum_sq_masked = grad_sum_sq.sparse_mask(grad)
                    s_masked = s.sparse_mask(grad)

                    # Compute x_0 from other known quantities
                    rms_masked_values = grad_sum_sq_masked._values().pow(1 / 3).add_(eps)
                    x0_masked_values = p_masked._values().addcdiv(s_masked._values(), rms_masked_values, value=1)

                    # Dense + sparse op
                    grad_sq = grad * grad
                    grad_sum_sq.add_(grad_sq, alpha=_lambda)
                    grad_sum_sq_masked.add_(grad_sq, alpha=_lambda)

                    rms_masked_values = grad_sum_sq_masked._values().pow_(1 / 3).add_(eps)
                    if eps == 0.0:
                        rms_masked_values[rms_masked_values == 0] = float('inf')

                    s.add_(grad, alpha=_lambda)
                    s_masked._values().add_(grad._values(), alpha=_lambda)

                    # update masked copy of p
                    p_kp1_masked_values = x0_masked_values.addcdiv(s_masked._values(), rms_masked_values, value=-1)

                    # Copy updated masked p to dense p using an add operation
                    p_masked._values().add_(p_kp1_masked_values, alpha=-1)
                    p.data.add_(p_masked, alpha=-1)
                else:
                    if momentum == 0.0:
                        # Compute x_0 from other known quantities
                        rms = grad_sum_sq.pow(1 / 3).add_(eps)
                        x0 = p.addcdiv(s, rms, value=1)
                    else:
                        x0 = state['x0']

                    # Accumulate second moments
                    grad_sum_sq.addcmul_(grad, grad, value=_lambda)
                    rms = grad_sum_sq.pow(1 / 3).add_(eps)

                    if eps == 0.0:
                        rms[rms == 0] = float('inf')

                    s.add_(grad, alpha=_lambda)

                    if weight_decay > 0.0 and self.decouple_decay:
                        p_old = p.clone()

                    if momentum == 0.0:
                        p.copy_(x0.addcdiv(s, rms, value=-1))
                    else:
                        z = x0.addcdiv(s, rms, value=-1)
                        p.mul_(momentum).add_(z, alpha=1.0 - momentum)

                    if weight_decay > 0.0 and self.decouple_decay:
                        p.add_(p_old, alpha=-lr * weight_decay)

        self.state['k'] += 1

        return loss
