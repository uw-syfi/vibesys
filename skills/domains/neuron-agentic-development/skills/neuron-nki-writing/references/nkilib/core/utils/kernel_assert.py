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


def kernel_assert(condition: bool, error_text: str):
    assert condition, f"[INTERNAL_ERROR] [NCC_INKI016] Kernel validation exception: {error_text} - Please check the validation message and adjust kernel inputs accordingly"  # noqa: S101


def assert_shape(tensor, expected_shape, tensor_name, error_text=""):
    """Assert tensor shape matches expected shape, providing detailed error message."""
    kernel_assert(
        tensor.shape == expected_shape,
        f"Received unexpected shape for {tensor_name}. "
        f"Expected {expected_shape}, received {tensor.shape}. {error_text}",
    )
