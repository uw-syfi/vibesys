# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License").
# You may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import torch


def layer_norm_torch_ref(hidden, gamma, norm_b=None, eps=1e-6, **_):
    """
    PyTorch reference implementation of Layer normalization.

    Args:
        hidden: Input tensor to normalize.
        gamma: Scale parameter (optional).
        norm_b: Bias parameter (optional).
        eps: Epsilon for numerical stability.

    Returns:
        Normalized tensor.
    """
    # All intermediates need to happen in FP32 for numerical precision
    hidden = hidden.to(torch.float32)

    mean = hidden.mean(dim=-1, keepdim=True)
    var = hidden.var(dim=-1, correction=0, keepdim=True)

    norm = (hidden - mean) * (var + eps).sqrt().reciprocal().to(hidden.dtype)
    if gamma is not None:
        norm *= gamma
    if norm_b is not None:
        norm += norm_b
    return norm
