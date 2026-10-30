# NCC_EVRF019

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EVRF019
**Error message**: The compiler encountered a reduce-window operation with more or less than 2 operands. Support for reduce_window is available for exactly one input tensor and one initial value for reduction.

Erroneous code example:


```python
# reduce-window operation with more or less than 2 operands is not supported
# 4 operands are being provided instead of 2
lax.reduce_window(
    (x, x),               # ERROR: a tuple of two input tensors
    (-jnp.inf, jnp.inf),  # ERROR: a tuple of two initial values
    lambda a, b: (jnp.maximum(a[0], b[0]), jnp.minimum(a[1], b[1])),
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 2, 2, 1),
    padding='VALID'
)
```


If possible, split multi-operand reduce_window with multiple single-operand reduce_window operations.


```python
# For max pooling
# 2 operands are correctly being provided
max_pool = lax.reduce_window(
    x,         # FIXED: a single input tensor
    -jnp.inf,  # FIXED: a single initial value
    lax.max,
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 2, 2, 1),
    padding='VALID'
)

# For min pooling
# 2 operands are correctly being provided
min_pool = lax.reduce_window(
    x,        # FIXED: a single input tensor
    jnp.inf,  # FIXED: a single initial value
    lax.min,
    window_dimensions=(1, 2, 2, 1),
    window_strides=(1, 2, 2, 1),
    padding='VALID'
)
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`