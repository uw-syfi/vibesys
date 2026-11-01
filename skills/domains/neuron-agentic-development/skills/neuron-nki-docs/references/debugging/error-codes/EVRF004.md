# NCC_EVRF004

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EVRF004
**Error message**: Complex data types are not supported on the Neuron device.

You cannot use complex data types (such as `complex64`, `complex128`, and others) on the Neuron device directly.

One fix is to offload complex operations to CPU, like so:


```python
x = torch.tensor([1+2j, 3+4j], dtype=torch.complex64).to('cpu')
```


> **Note**
>
> Note
> 
> 
> Since data transfer between CPU and device is expensive, this is best used when complex operations are rare.

You can also address this error by manually emulating complex tensors using real and imaginary parts:


```python
real = x.real
imag = x.imag
...
# (a + bi) * (c + di)
real_out = a_real * b_real - a_imag * b_imag
imag_out = a_real * b_imag + a_imag * b_real
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`