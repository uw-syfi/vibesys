# NCC_EVRF022

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EVRF022
**Error message**: Shift-right-arithmetic operation on non 32-bit inputs is not supported. Cast the first argument’s data type to be S32, U32, or F32.

Erroneous code example:


```python
def forward(self, input, other):
    return torch.bitwise_right_shift(input, other)

# This will be the first argument and must be 32-bit
input = torch.tensor([16, 32, 64], dtype=torch.int16)
# The second argument can be non 32-bit
other = torch.tensor([1, 2, 3], dtype=torch.int16)
```


To fix this error:


```python
def forward(self, input, other):
    return torch.bitwise_right_shift(input, other)

# Correctly setting the first argument to be 32-bit
input = torch.tensor([16, 32, 64], dtype=torch.int32)
other = torch.tensor([1, 2, 3], dtype=torch.int16)
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`