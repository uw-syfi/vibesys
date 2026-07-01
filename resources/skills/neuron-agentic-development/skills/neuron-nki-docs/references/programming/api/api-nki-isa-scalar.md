# NKI ISA - Scalar Engine

> **Module**: nki.isa
> **Total Functions**: 4

## Overview

Scalar Engine instructions.

## Functions

### nki.isa.activation {#nki-isa-activation}

# nki.isa.activation

nki.isa.activation

nki.isa.activation(*dst*, *op*, *data*, *bias=None*, *scale=1.0*, *reduce_op=None*, *reduce_res=None*, *reduce_cmd=reduce_cmd.idle*, *name=None*)[[source]](../../../_modules/nki/isa.html#activation)
Apply an activation function on every element of the input tile using Scalar Engine, with an optional scale/bias operation
before the activation and an optional reduction operation after the activation in the same instruction.

The activation function is specified in the `op` input field (see [Supported Activation Functions for NKI ISA](nki.api.shared.md#nki-act-func) for a list of
supported activation functions and their valid input ranges).

`nisa.activation` can optionally multiply the input `data` by a scalar or vector `scale`
and then add another vector `bias` before the activation function is applied.

After the activation function
is applied, Scalar Engine can also reduce along the free dimensions of the activated data per lane, using
`reduce_op` operation. `reduce_op` must be `nl.add`.

The reduction result is then either stored into or reduced on top of a set of internal engine registers
called `reduce_regs` (one 32-bit register per compute lane, 128 registers in total), controlled by the
`reduce_cmd` field:

* `nisa.reduce_cmd.reset`: Reset `reduce_regs` to zero only.

* `nisa.reduce_cmd.idle`: Do not modify `reduce_regs`.

* `nisa.reduce_cmd.reduce`: Reduce activated data over existing values in `reduce_regs`.

* `nisa.reduce_cmd.reset_reduce`: Reset `reduce_regs` to zero and then store the reduction result
of the activated data.

`nisa.activation` can also emit another instruction to read out `reduce_regs` by
passing an SBUF/PSUM tile in the `reduce_res` arguments.
The `reduce_regs` state can persist across multiple `nisa.activation` instructions without the need to
be evicted back to SBUF/PSUM (`reduce_res` tile).

The following is the pseudo code for `nisa.activation`:

\[ \begin{align}\begin{aligned}output = op(data * scale + bias)\\if reduce_cmd == nisa.reduce_cmd.reset or reduce_cmd == nisa.reduce_cmd.reset_reduce:
 reduce_regs = 0\\result = reduce\_op(reduce_regs, reduce\_op(output, axis=<FreeAxis>))\\if reduce_cmd == nisa.reduce_cmd.reduce or reduce_cmd == nisa.reduce_cmd.reset_reduce:
 reduce_regs += result\\if reduce_res:
 reduce_res = reduce_regs\end{aligned}\end{align} \]
All these optional operations incur no further performance penalty compared to only applying the activation function,
except reading out `reduce_regs` into `reduce_res` will have a small overhead due to an extra instruction.

**Memory types.**

The input `data` tile can be an SBUF or PSUM tile. Similarly, the instruction
can write the output `dst` tile into either SBUF or PSUM.

**Data types.**

Both input `data` and output `dst` tiles can be in any valid NKI data type
(see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
The Scalar Engine always performs the math operations in float32 precision.
Therefore, the engine automatically casts the input `data` tile to float32 before
performing multiply/add/activate specified in the activation instruction.
The engine is also capable of casting the float32 math results into another
output data type in `dst` at no additional performance cost.
The `scale` parameter must
have a float32 data type, while the `bias` parameter can be float32/float16/bfloat16.

**Layout.**

The `scale` can either be a compile-time constant scalar or a
`[N, 1]` vector from SBUF/PSUM. `N` must be the same as the partition dimension size of `data`.
In NeuronCore-v2, the `bias` must be a `[N, 1]` vector, but starting NeuronCore-v3, `bias` can either be
a compile-time constant scalar or a `[N, 1]` vector similar to `scale`.

When the `scale` (or similarly, `bias`) is a scalar, the scalar
is broadcasted to all the elements in the input `data` tile to perform the computation.
When the `scale` (or `bias`) is a vector, the `scale` (or `bias`) value in each partition is broadcast
along the free dimension of the `data` tile.

**Tile size.**

The partition dimension size of input `data` and output `dst` tiles must be the same and must not exceed 128.
The number of elements per partition of `data` and `dst` tiles must be the same and must not
exceed the physical size of each SBUF partition.

Parameters:

* **dst** – the activation output

* **op** – an activation function (see [Supported Activation Functions for NKI ISA](nki.api.shared.md#nki-act-func) for supported functions)

* **data** – the input tile; layout: (partition axis <= 128, free axis)

* **scale** – a scalar or a vector for multiplication

* **bias** – a scalar (NeuronCore-v3 or newer) or a vector for addition

* **reduce_op** – the reduce operation to perform on the free dimension of the activated data

* **reduce_res** – a tile of shape `(data.shape[0], 1)` to hold the final state of `reduce_regs`.

* **reduce_cmd** – an enum member from `nisa.reduce_cmd` to control the state of `reduce_regs`.

---

### nki.isa.activate2 {#nki-isa-activate2}

# nki.isa.activate2

nki.isa.activate2

nki.isa.activate2(*dst*, *op*, *data*, *imm0*, *imm1*, *op0*, *op1*, *relu_param=0.0*, *reverse0=False*, *reverse1=False*, *reduce_op=None*, *reduce_res=None*, *reduce_cmd=reduce_cmd.idle*, *name=None*)

Perform tensor activation with configurable tensor-scalar operations and optional reduction using Scalar Engine. Available only on NeuronCore-v4 (trn3) and newer.

This instruction provides a three-stage pipeline per partition:

1. Tensor-scalar operations: `(data op0 imm0) op1 imm1`
2. Activation function application via `op`
3. Optional internal reduction controlled by `reduce_op` and `reduce_cmd`

The tensor-scalar stage supports six `(op0, op1)` combinations:

- `(nl.multiply, nl.add)` — scale and bias
- `(nl.multiply, nl.subtract)` — scale and negative bias
- `(nl.multiply, nl.bypass)` — scale only
- `(nl.add, nl.bypass)` — bias only
- `(nl.subtract, nl.bypass)` — subtract only
- `(nl.bypass, nl.bypass)` — no tensor-scalar operation

When `reverse0=True`, the first operation computes `imm0 <op0> data` instead of `data <op0> imm0`. Similarly, `reverse1=True` computes `imm1 <op1> result`.

Parameters:

* **dst** – the activation output tile. Supported buffers: SBUF, PSUM.
* **op** – an activation function (e.g., `nl.exp`, `nl.sigmoid`, `nl.prelu`, `nl.bypass`).
* **data** – the input tile; layout: (partition axis <= 128, free axis). Supported buffers: SBUF, PSUM.
* **imm0** – scalar or `[N, 1]` vector value for the first tensor-scalar operation.
* **imm1** – scalar or `[N, 1]` vector value for the second tensor-scalar operation.
* **op0** – first ALU operation (`nl.multiply`, `nl.add`, `nl.subtract`, or `nl.bypass`).
* **op1** – second ALU operation (`nl.add`, `nl.subtract`, or `nl.bypass`).
* **relu_param** – parameter for PReLU activation. Defaults to 0.0.
* **reverse0** – if True, compute `imm0 <op0> data` instead of `data <op0> imm0`.
* **reverse1** – if True, compute `imm1 <op1> result` instead of `result <op1> imm1`.
* **reduce_op** – optional reduction operator (e.g., `nl.add`, `nl.maximum`).
* **reduce_res** – destination tile for reduction result.
* **reduce_cmd** – reduction command (`nisa.reduce_cmd.idle`, `.reset_reduce`, `.accumulate_reduce`).

---

### nki.isa.activation_reduce {#nki-isa-activation_reduce}

# nki.isa.activation_reduce

nki.isa.activation_reduce

nki.isa.activation_reduce(*dst*, *op*, *data*, *reduce_op*, *reduce_res*, *bias=None*, *scale=1.0*, *name=None*)[[source]](../../../_modules/nki/isa.html#activation_reduce)
Perform the same computation as `nisa.activation` and also a reduction along the free dimension of the
`nisa.activation` result using Scalar Engine. The results for the reduction is stored
in the reduce_res.

This API is equivalent to calling `nisa.activation` with
`reduce_cmd=nisa.reduce_cmd.reset_reduce` and passing in reduce_res. This API is kept for
backward compatibility, we recommend using `nisa.activation` moving forward.

Refer to [nisa.activation](nki.isa.activation.md) for semantics of `op/data/bias/scale`.

In addition to [nisa.activation](nki.isa.activation.md) computation, this API also performs a reduction
along the free dimension(s) of the [nisa.activation](nki.isa.activation.md) result, at a small additional
performance cost. The reduction result is returned in `reduce_res` in-place, which must be a
SBUF/PSUM tile with the same partition axis size as the input tile `data` and one element per partition.
On NeuronCore-v2, the `reduce_op` must be `nl.add`.

There are 128 registers on the scalar engine for storing reduction results, corresponding
to the 128 partitions of the input. These registers are shared between `activation` and `activation_accu` calls.
This instruction first resets those
registers to zero, performs the reduction on the value after activation function is applied,
stores the results into the registers,
then reads out the reduction results from the register, eventually store them into `reduce_res`.

Note that `nisa.activation` can also change the state of the register. It’s user’s
responsibility to ensure correct ordering. It’s the best practice to not mixing
the use of `activation_reduce` and `activation`.

Reduction axis is not configurable in this API. If the input tile has multiple free axis, the API will
reduce across all of them.

Mathematically, this API performs the following computation:

\[\begin{split}output = f_{act}(data * scale + bias) \\
reduce\_res = reduce\_op(output, axis=<FreeAxis>)\end{split}\]

Parameters:

* **dst** – output tile of the activation instruction; layout: same as input `data` tile

* **op** – an activation function (see [Supported Activation Functions for NKI ISA](nki.api.shared.md#nki-act-func) for supported functions)

* **data** – the input tile; layout: (partition axis <= 128, free axis)

* **reduce_op** – the reduce operation to perform on the free dimension of the activation result

* **reduce_res** – a tile of shape `(data.shape[0], 1)`, where data.shape[0]
is the partition axis size of the input `data` tile. The result of `sum(ReductionResult)`
is written in-place into the tensor.

* **bias** – a vector with the same partition axis size as `data`
for broadcast add (after broadcast multiply with `scale`)

* **scale** – a scalar or a vector with the same partition axis size as `data`
for broadcast multiply

---

### nki.isa.dropout {#nki-isa-dropout}

# nki.isa.dropout

nki.isa.dropout

nki.isa.dropout(*dst*, *data*, *prob*, *name=None*)[[source]](../../../_modules/nki/isa.html#dropout)
Randomly replace some elements of the input tile `data` with zeros
based on input probabilities using Vector Engine.
The probability of replacing input elements with zeros (i.e., drop probability)
is specified using the `prob` field:
- If the probability is 1.0, all elements are replaced with zeros.
- If the probability is 0.0, all elements are kept with their original values.

The `prob` field can be a scalar constant or a tile of shape `(data.shape[0], 1)`,
where each partition contains one drop probability value.
The drop probability value in each partition is applicable to the input
`data` elements from the same partition only.

Data type of the input `data` tile can be any valid NKI data types
(see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
However, data type of `prob` has restrictions based on the data type of `data`:

* If data type of `data` is any of the integer types (e.g., int32, int16),
`prob` data type must be float32

* If data type of data is any of the float types (e.g., float32, bfloat16),
`prob` data can be any valid float type

The output data type `dst.dtype` must match the input data type `data.dtype`.

Parameters:

* **dst** – an output tile of the dropout result

* **data** – the input tile

* **prob** – a scalar or a tile of shape `(data.shape[0], 1)` to indicate the
probability of replacing elements with zeros

---

### nki.isa.reciprocal {#nki-isa-reciprocal}

# nki.isa.reciprocal

nki.isa.reciprocal

nki.isa.reciprocal(*dst*, *data*, *name=None*)[[source]](../../../_modules/nki/isa.html#reciprocal)
Compute element-wise reciprocal (1.0/x) of the input `data` tile using Vector Engine.

**Memory types.**

Both the input `data` and output `dst` tiles can be in SBUF or PSUM.

**Data types.**

The input `data` tile can be any valid NKI data type (see [Supported Data Types](nki.api.shared.md#nki-dtype) for more information).
The Vector Engine automatically casts the input data type to float32 and performs the reciprocal
computation in float32 math. The float32 results are cast to the data type of `dst`.

**Layout.**

The partition dimension of the input `data` is considered the parallel compute dimension.

**Tile size.**

The partition dimension size of input `data` and output `dst` tiles must be the same
and must not exceed 128. The number of elements per partition of `dst` must match
that of `data` and must not exceed the physical size of each SBUF partition.

Parameters:

* **dst** – the output tile

* **data** – the input tile

---
