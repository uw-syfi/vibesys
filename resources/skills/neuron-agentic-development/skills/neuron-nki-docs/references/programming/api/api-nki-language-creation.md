# NKI Language - Array Creation

> **Module**: nki.language
> **Total Functions**: 2

## Overview

Functions for creating and initializing arrays and tensors.

## Functions

### nki.language.ndarray {#nki-language-ndarray}

# nki.language.ndarray

nki.language.ndarray

nki.language.ndarray(*shape*, *dtype*, *buffer=nl.sbuf*, *name=''*, *address=None*)[[source]](../../../_modules/nki/language.html#ndarray)
Create a new tensor of given shape and dtype on the specified buffer.

((Similar to [numpy.ndarray](https://numpy.org/doc/stable/reference/generated/numpy.ndarray.html)))

Parameters:

* **shape** – the shape of the tensor.

* **dtype** – the data type of the tensor (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).

* **buffer** – the specific buffer (ie, [sbuf](nki.language.sbuf.md), [psum](nki.language.psum.md), [hbm](nki.language.hbm.md)), defaults to [sbuf](nki.language.sbuf.md). String buffer names are no longer accepted in NKI 0.3.0.

* **name** – the name of the tensor.

* **address** – (optional) explicit memory placement as `(partition_offset, free_offset)` tuple. New in NKI 0.3.0.

Returns:
a new tensor allocated on the buffer.

---

### nki.language.zeros {#nki-language-zeros}

# nki.language.zeros

nki.language.zeros

nki.language.zeros(*shape*, *dtype*, *buffer=nl.sbuf*, *name=''*)[[source]](../../../_modules/nki/language.html#zeros)
Create a new tensor of given shape and dtype on the specified buffer, filled with zeros.

((Similar to [numpy.zeros](https://numpy.org/doc/stable/reference/generated/numpy.zeros.html)))

Parameters:

* **shape** – the shape of the tensor.

* **dtype** – the data type of the tensor (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).

* **buffer** – the specific buffer (ie, [sbuf](nki.language.sbuf.md), [psum](nki.language.psum.md), [hbm](nki.language.hbm.md)), defaults to [sbuf](nki.language.sbuf.md).

* **name** – the name of the tensor.

Returns:
a new tensor allocated on the buffer.

---
