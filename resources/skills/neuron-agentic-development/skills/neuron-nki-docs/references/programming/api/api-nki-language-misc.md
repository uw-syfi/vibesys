# NKI Language - Miscellaneous

> **Module**: nki.language
> **Total Functions**: 12

## Overview

Other language functions.

## Functions

### nki.language.bool_ {#nki-language-bool_}

# nki.language.bool_

nki.language.bool_

nki.language.bool_* = 'bool'*
Boolean (True or False) stored as a byte

---

### nki.language.device_print {#nki-language-device_print}

# nki.language.device_print

nki.language.device_print

nki.language.device_print(*print_prefix*, *tensor*)[[source]](../../../_modules/nki/language.html#device_print)
Print a message with a string `print_prefix` followed by the value of a tile `tensor`.

By default, using this function will not result in your tensors being printed out. When running your kernel,
you need to define the environment variable `NEURON_RT_DEBUG_OUTPUT_DIR` and point it to a directory that will
store the tensor data grouped by prefix each time the device_print instruction is executed.

The structure of the directory will be `<print_prefix>/core_<logical core id>/<iteration>/...`.

Listing 12 Example usage

```python
import nki.isa as nisa
import nki.language as nl

def my_nki_kernel(input_tensor):
    a_tile = sbuf.view(input_tensor.dtype, input_tensor.shape)
    nisa.dma_copy(a_tile, input_tensor)
    nl.device_print("a_tile", a_tile)

    ...
```


> **Note**
>
> Warning
> 
> 
> This feature is only available when using the NxD Inference library.

Parameters:

* **print_prefix** ([*str*](https://docs.python.org/3/library/stdtypes.html#str)) – prefix of the print message. This string is evaluated at trace time and must be a constant expression.

* **tensor** – tensor to print out. Can be in SBUF or HBM.

Returns:
None

---

### nki.language.ds {#nki-language-ds}

# nki.language.ds

nki.language.ds

nki.language.ds(*start*, *size*)[[source]](../../../_modules/nki/language.html#ds)
Construct a dynamic slice for simple tensor indexing.


```python
import nki.language as nl
import nki.isa as nisa
...



@nki.jit
def example_kernel(in_tensor):
  out_tensor = nl.ndarray(in_tensor.shape, dtype=in_tensor.dtype,
                          buffer=nl.shared_hbm)
  for i in range(in_tensor.shape[1] // 512):
    tile = nl.ndarray((128, 512), dtype=in_tensor.dtype, buffer=nl.sbuf)
    nisa.dma_copy(dst=tile, src=in_tensor[:, (i * 512):((i + 1) * 512)])
    # Same as above but use ds (dynamic slice) instead of the native
    # slice syntax
    nisa.dma_copy(dst=tile, src=in_tensor[:, nl.ds(i * 512, 512)])
```

---

### nki.language.float16 {#nki-language-float16}

# nki.language.float16

nki.language.float16

nki.language.float16* = 'float16'*
16-bit floating-point number

---

### nki.language.float32 {#nki-language-float32}

# nki.language.float32

nki.language.float32

nki.language.float32* = 'float32'*
32-bit floating-point number

---

### nki.language.float4_e2m1fn_x4 {#nki-language-float4_e2m1fn_x4}

# nki.language.float4_e2m1fn_x4

nki.language.float4_e2m1fn_x4

nki.language.float4_e2m1fn_x4* = 'float4_e2m1fn_x4'*
4x packed float4_e2m1fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4

---

### nki.language.int16 {#nki-language-int16}

# nki.language.int16

nki.language.int16

nki.language.int16* = 'int16'*
16-bit signed integer number

---

### nki.language.int32 {#nki-language-int32}

# nki.language.int32

nki.language.int32

nki.language.int32* = 'int32'*
32-bit signed integer number

---

### nki.language.int8 {#nki-language-int8}

# nki.language.int8

nki.language.int8

nki.language.int8* = 'int8'*
8-bit signed integer number

---

### nki.language.uint16 {#nki-language-uint16}

# nki.language.uint16

nki.language.uint16

nki.language.uint16* = 'uint16'*
16-bit unsigned integer number

---

### nki.language.uint32 {#nki-language-uint32}

# nki.language.uint32

nki.language.uint32

nki.language.uint32* = 'uint32'*
32-bit unsigned integer number

---

### nki.language.uint8 {#nki-language-uint8}

# nki.language.uint8

nki.language.uint8

nki.language.uint8* = 'uint8'*
8-bit unsigned integer number

---
