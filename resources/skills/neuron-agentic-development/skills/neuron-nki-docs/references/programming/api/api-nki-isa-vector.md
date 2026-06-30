# NKI ISA - Vector Engine

> **Module**: nki.isa
> **Total Functions**: 2

## Overview

Vector Engine instructions.

## Functions

### nki.isa.bn_aggr {#nki-isa-bn_aggr}

# nki.isa.bn_aggr

nki.isa.bn_aggr

nki.isa.bn_aggr(*dst*, *data*, *name=None*)[[source]](../../../_modules/nki/isa.html#bn_aggr)
Aggregate one or multiple `bn_stats` outputs to generate
a mean and variance per partition using Vector Engine.

The input `data` tile
effectively has an array of `(count, mean, variance*count)` tuples per partition
produced by [bn_stats](nki.isa.bn_stats.md) instructions. Therefore, the number of elements per partition
of `data` must be a modulo of three.

Note, if you need to aggregate multiple `bn_stats` instruction outputs,
it is recommended to declare a SBUF tensor
and then make each `bn_stats` instruction write its output into the
SBUF tensor at different offsets.

Vector Engine performs the statistics aggregation in float32 precision.
The engine automatically casts the input `data` to float32 before performing computation.
The float32 computation results are cast to `dst.dtype` at no additional performance cost.

Parameters:

* **dst** – an output tile with two elements per partition: a mean followed by a variance

* **data** – an input tile with results of one or more [bn_stats](nki.isa.bn_stats.md)

---

### nki.isa.bn_stats {#nki-isa-bn_stats}

# nki.isa.bn_stats

nki.isa.bn_stats

nki.isa.bn_stats(*dst*, *data*, *name=None*)[[source]](../../../_modules/nki/isa.html#bn_stats)
Compute mean- and variance-related statistics for each partition of an input tile `data`
in parallel using Vector Engine.

The output tile of the instruction has 6 elements per partition:

* the `count` of the even elements (of the input tile elements from the same partition)

* the `mean` of the even elements

* `variance * count` of the even elements

* the `count` of the odd elements

* the `mean` of the odd elements

* `variance * count` of the odd elements

To get the final mean and variance of the input tile,
we need to pass the above `bn_stats` instruction output
into the [bn_aggr](nki.isa.bn_aggr.md)
instruction, which will output two elements per partition:

* mean (of the original input tile elements from the same partition)

* variance

Due to hardware limitation, the number of elements per partition
(i.e., free dimension size) of the input `data` must not exceed 512 (nl.tile_size.bn_stats_fmax).
To calculate per-partition mean/variance of a tensor with more than
512 elements in free dimension, we can invoke `bn_stats` instructions
on each 512-element tile and use a single `bn_aggr` instruction to
aggregate `bn_stats` outputs from all the tiles.

Vector Engine performs the above statistics calculation in float32 precision.
The engine automatically casts the input `data` to float32 before performing computation.
The float32 computation results are cast to `dst.dtype` at no additional performance cost.

Parameters:

* **dst** – an output tile with 6-element statistics per partition

* **data** – the input tile (up to 512 elements per partition)

---
