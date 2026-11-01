# NCC_EVRF013

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EVRF013
**Error message**: TopK does not support int32 or int64 input tensors.

Erroneous code example:


```python
def forward(self, x):
    # assume x is an integer tensor
    # error: cannot call TopK on integer dtypes
    k = 5
    values, indices = torch.topk(x, k=k, dim=-1)
    return values, indices
```


To fix this error, you can cast your tensor to a supported floating point dtype.


```python
def forward(self, x):
    x = x.float()
    k = 5
    values, indices = torch.topk(x, k=k, dim=-1)
    return values, indices
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`