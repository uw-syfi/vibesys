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


from enum import Enum


class QKVOutputLayout(Enum):
    BSD = 0  # (b, s, (n_q_heads + 2 * n_kv_heads) * d_head)
    NBSd = 1  # (num_heads, b, s, d_head)
    NBdS = 2  # (num_heads, b, d_head, s)


class NormType(Enum):
    NO_NORM = 0
    RMS_NORM = 1
    LAYER_NORM = 2
    RMS_NORM_SKIP_GAMMA = 3


class ActFnType(Enum):
    SiLU = 0
    GELU = 1
    GELU_Tanh_Approx = 2
    Swish = 3


class RouterActFnType(Enum):
    """Supported activation types for RouterTopK kernel"""

    SIGMOID = 0
    SOFTMAX = 1

    def __str__(self):
        return self.name.lower()


class ExpertAffinityScaleMode(Enum):
    NO_SCALE = 0
    POST_SCALE = 1
    PRE_SCALE = 2
    PRE_SCALE_DELAYED = 3


class QuantizationType(Enum):
    NONE = 0
    STATIC = 1
    ROW = 2
    MX = 3


class GateUpDim(Enum):
    GATE = 0
    UP = 1
