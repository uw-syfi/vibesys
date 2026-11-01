# nki.language — Data Types

> **Module**: nki.language
> **Functions**: 20

## Overview

Data Types instructions.

## Functions

### nki.language.bfloat16 {#nki-language-bfloat16}

`nki.language.bfloat16()`

**Signature:**
```python
language.bfloat16
```

16-bit floating-point number (1S,8E,7M)

---

### nki.language.bool_ {#nki-language-bool_}

`nki.language.bool_()`

**Signature:**
```python
language.bool_
```

Boolean (True or False) stored as a byte

---

### nki.language.float16 {#nki-language-float16}

`nki.language.float16()`

**Signature:**
```python
language.float16
```

16-bit floating-point number

---

### nki.language.float32 {#nki-language-float32}

`nki.language.float32()`

**Signature:**
```python
language.float32
```

32-bit floating-point number

---

### nki.language.float4_e2m1fn_x4 {#nki-language-float4_e2m1fn_x4}

`nki.language.float4_e2m1fn_x4()`

**Signature:**
```python
language.float4_e2m1fn_x4
```

4x packed float4_e2m1fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4

---

### nki.language.float8_e4m3 {#nki-language-float8_e4m3}

`nki.language.float8_e4m3()`

**Signature:**
```python
language.float8_e4m3
```

8-bit floating-point number (1S,4E,3M)

---

### nki.language.float8_e4m3fn {#nki-language-float8_e4m3fn}

`nki.language.float8_e4m3fn()`

**Signature:**
```python
language.float8_e4m3fn
```

8-bit floating-point number (1S,4E,3M), Extended range: no inf, NaN represented by 0bS111'1111

---

### nki.language.float8_e4m3fn_x4 {#nki-language-float8_e4m3fn_x4}

`nki.language.float8_e4m3fn_x4()`

**Signature:**
```python
language.float8_e4m3fn_x4
```

4x packed float8_e4m3fn elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4

---

### nki.language.float8_e5m2 {#nki-language-float8_e5m2}

`nki.language.float8_e5m2()`

**Signature:**
```python
language.float8_e5m2
```

8-bit floating-point number (1S,5E,2M)

---

### nki.language.float8_e5m2_x4 {#nki-language-float8_e5m2_x4}

`nki.language.float8_e5m2_x4()`

**Signature:**
```python
language.float8_e5m2_x4
```

4x packed float8_e5m2 elements, custom data type for nki.isa.nc_matmul_mx on NeuronCore-v4

---

### nki.language.float8_e8m0fnu {#nki-language-float8_e8m0fnu}

`nki.language.float8_e8m0fnu()`

**Signature:**
```python
language.float8_e8m0fnu
```

8-bit floating-point exponent type (0S,8E,0M) - unsigned, NaN represented by 0xFF. Used as power-of-two shared scale factor in MX quantization

---

### nki.language.int16 {#nki-language-int16}

`nki.language.int16()`

**Signature:**
```python
language.int16
```

16-bit signed integer number

---

### nki.language.int32 {#nki-language-int32}

`nki.language.int32()`

**Signature:**
```python
language.int32
```

32-bit signed integer number

---

### nki.language.int64 {#nki-language-int64}

`nki.language.int64()`

**Signature:**
```python
language.int64
```

64-bit signed integer number

---

### nki.language.int8 {#nki-language-int8}

`nki.language.int8()`

**Signature:**
```python
language.int8
```

8-bit signed integer number

---

### nki.language.tfloat32 {#nki-language-tfloat32}

`nki.language.tfloat32()`

**Signature:**
```python
language.tfloat32
```

32-bit floating-point number (1S,8E,10M)

---

### nki.language.uint16 {#nki-language-uint16}

`nki.language.uint16()`

**Signature:**
```python
language.uint16
```

16-bit unsigned integer number

---

### nki.language.uint32 {#nki-language-uint32}

`nki.language.uint32()`

**Signature:**
```python
language.uint32
```

32-bit unsigned integer number

---

### nki.language.uint64 {#nki-language-uint64}

`nki.language.uint64()`

**Signature:**
```python
language.uint64
```

64-bit unsigned integer number

---

### nki.language.uint8 {#nki-language-uint8}

`nki.language.uint8()`

**Signature:**
```python
language.uint8
```

8-bit unsigned integer number

---

