# NCC_EVRF010

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EVRF010
**Error message**: The compiler encountered simultaneous use of input and kernel dilation, which is not supported.

Erroneous code example:


```python
x = jnp.ones((1, 4, 4, 1), dtype=jnp.float32)
kernel = jnp.ones((3, 3, 1, 1), dtype=jnp.float32)

result = lax.conv_general_dilated(
    x,
    kernel,
    window_strides=(1, 1),
    padding=((2, 2), (2, 2)),
    lhs_dilation=(2, 2), # input dilation
    rhs_dilation=(2, 2), # kernel dilation
    dimension_numbers=('NHWC', 'HWIO', 'NHWC')
)
```


If possible, use only only input or kernel dilation:


```python
x = jnp.ones((1, 4, 4, 1), dtype=jnp.float32)
kernel = jnp.ones((3, 3, 1, 1), dtype=jnp.float32)

result = lax.conv_general_dilated(
    x,
    kernel,
    window_strides=(1, 1),
    padding=((2, 2), (2, 2)),
    lhs_dilation=(1, 1), # no input dilation
    rhs_dilation=(2, 2),
    dimension_numbers=('NHWC', 'HWIO', 'NHWC')
)
```


Or apply dilation manually and apply convolution to the remainder.

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`