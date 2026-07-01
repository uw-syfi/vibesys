# NKI Access Patterns

NKI Access Patterns
NKI supports the use of access patterns (AP) on
`nl.ndarray`, which provides users with the ability to specify
hardware-native access patterns. This low-level capability allows developers
to specify precisely what they want their instructions to read on the hardware.

Access patterns are only necessary if slicing cannot represent the desired
tensor access.

## Hardware Capability

Instructions can read and write tensors from/to the SBUF or PSUM, which are
both two-dimensional memories with 128 partitions on NeuronCore v2/v3/v4.
Within each SBUF/PSUM partition, the tensor read/write logic on the NeuronCore
supports accessing elements from up to four-dimensional arrays, though most
instructions only support 1D/2D/3D in the free dimension due to instruction
length limitations.

The multi-dimensional access patterns are typically described using two pieces
of information: 1) the element stepping (i.e., `stride`) and 2) number of
elements (i.e., `size`) in each dimension. A tensor access pattern of an
instruction is expected to be the same across all partitions.

In addition to the free dimension pattern, additional information is required
to locate the number of elements to access: 1) the offset from the beginning
of the tensor and 2) the number of partitions. The next section will describe
how the NKI API abstracts this information.

## NKI API for the Access Pattern

The NKI API for access pattern is a direct reflection of the hardware capability.
The `nl.ndarray` has an `ap` method.


```python
def ap(self, pattern: List[Tuple[int, int]],
   offset: Optional[int] = 0,
   scalar_offset: Optional[Access] = None,
   vector_offset: Optional[Access] = None,
   indirect_dim: int = 0
   dtype: Optional[Dtype] = None):
   pass
```


The parameters have the following definitions:

* `pattern`: A list of two-element tuples, each tuple describes the access on one dimension. The first element represents the element stepping and the second element represents the number of elements in each dimension. This tuple is referred to as `[step, num]` going forward.

The shape of a pattern is the collection of num. For example, given pattern `[[w_step, w_num], [z_step, z_num], [y_step, y_num], [x_step, x_num]]`, the shape is `[w_num, z_num, y_num, x_num]`.

* It is worth mentioning that the order of the pattern specified here is in the opposite order to what is actually accepted by the hardware. Therefore, the order of the tuples shown on the profiler will be to the opposite order of what is specified here.

* `offset`: The offset to start the access in terms of number of elements from the beginning of the tensor. The default value is 0.

* `scalar_offset`: An SBUF memory location that specifies the location to start the access in terms of number of elements on the `indirect_dim` of the access pattern. At most one of the `scalar_offset` and `vector_offset` can be specified.

* `vector_offset`: An SBUF memory location that specifies the location to start the access in terms of number of elements from the beginning of the indirect dimension specified by `indirect_dim`. At most one of the `scalar_offset` and `vector_offset` can be specified.

* `indirect_dim`: The indirect dimension on which to apply `scalar_offset` and `vector_offset`.

* `dtype`: The data type of the access pattern. The default value is the `dtype` of the tensor being accessed.

## Semantics of the Access Pattern

Access patterns can be thought of as compact representations of a loop. The
offset is an integer indicating the start offset in terms of elements with
respect to the beginning of the tensor. Each two-element list `[step, num]`
represents the stride in terms of elements and the number of iterations of
each level of the loop. The semantics are explored through the following
example.

Given a tensor, the Access Pattern conceptually flattens the tensor to 1d,
and then uses a loop to fetch elements from the tensor to construct a view.
Consider the following NKI code:


```python
t = nl.ndarray((p_count, N), dtype=nl.float32, buffer=nl.sbuf)
access = t.ap(
  pattern=[[N, p_size], [z_step, z_num], [
  y_step, y_num], [x_step, x_num]],
  offset)
```


The above represents the following access on the tensor `t`, written below in pseudo-code.


```python
access = nl.ndarray((p_size, z_num, y_num, x_num), dtype=nl.float32, buffer=nl.sbuf)
for w in range(p_size):
  for z in range(z_num):
    for y in range(y_num):
      for x in range(x_num):
        t_flatten = t.flatten() # first flatten the tensor to 1d
        access[w, z, y, x] = [offset + (w * N) + (z * z_step)
                  + (y * y_step) + (x * x_step)]
```


The access pattern has the following properties:

1. Recall from the hardware capability, the access pattern in each partition
must be identical. Therefore, the step of the first tuple in the AP must be
equal to the number of elements in the free dimension of the tensor.
2. The shape of the result view is always the same as the shape of the pattern.

Note that calling `.ap` on a tensor does not do any computation directly.
It describes how to get data. The engines will consume data when the AP
is passed into a `nki.isa` instruction.


```python
src = nl.ndarray((16, 32), dtype=nl.float32, buffer=nl.sbuf)
dst = nl.ndarray((16, 32), dtype=nl.float32, buffer=nl.sbuf)
src_access = src.ap([32, 16], [1, 32]) # no computation happens
dst_access = dst.ap([32, 16], [1, 32]) # no computation happens

# Engine reads both src_access and dst_access and performs the copy
nisa.dma_copy(dst_access, src_access)
```


## A Concrete Example

Given a tensor `t` of size (16P, 16F), to iterate all the elements in
`t[0:16, 8:16]` the access pattern can be written as:


```python
t = nl.ndarray((16, 16), dtype=nl.float32, buffer=nl.sbuf)
access = t.ap(pattern=[[16, 16], [1, 8]], offset=8)


# Semantics, the following is pseudo-code
access = nl.ndarray((16, 8), dtype=nl.float32, buffer=nl.sbuf)
# in loop form
for w in range(16):
  for z in range(8):
    idx = 8 + (w * 16) + (1 * z)
    t_flatten = t.flatten()
    access[w, z] = t_flatten[idx]
```



> **Figure: memory access visualization 1**
>
> A memory access pattern visualization showing a 16x16 element grid where half the columns (8-15) are accessed, demonstrating a strided memory access pattern with offset and step parameters used in NKI DMA operations.
>
> This diagram visualizes how NKI accesses memory elements in a structured pattern, useful for understanding DMA transfer patterns and memory layout optimization.
>
> **Title and Layout:**
> - Title: "Memory Access Visualization"
> - Grid: 16 rows (labeled 0-15) x 16 columns (labeled 0-15)
> - Total grid size: 256 elements
>
> **Memory Access Pattern:**
> The grid shows two distinct regions:
> 1. **Unaccessed region (columns 0-7)**: White/empty squares indicating elements not accessed
> 2. **Accessed region (columns 8-15)**: Green squares with white dots indicating accessed elements
>
> **Pattern Annotations:**
> - **"Offset=8"** (top, orange text with arrow): Points to column 8, indicating the starting column for access
> - **"In each row, read 8 elements, step=1 at a time"** (right side, orange text): Describes the access pattern within each row
> - **"Jump 16 elements at a time, repeat 16 times"** (left side, orange text with curved arrow): Describes how the pattern moves between rows
>
> **Access Pattern Details:**
> - Starts at column 8 (offset = 8)
> - Reads 8 consecutive elements per row (columns 8-15)
> - Steps by 1 within each row
> - Jumps 16 elements (one full row) to reach the next row
> - Repeats for all 16 rows
>
> **Legend (Bottom):**
> - Green circle with dot: "Accessed" - elements that are read/written
> - "N Overlapping" (orange): Indicates when elements are accessed multiple times
> - White square: "Unaccessed" - elements not accessed
>
> **Statistics (Bottom):**
> - Total elements: 128 (the accessed portion)
> - Index range: 8 - 255
> - Memory region: 16 x 16 = 256 elements
>
> **Key Elements:**
> - **Offset=8**: Starting position for memory access
> - **8 elements per row**: Width of access pattern
> - **16 row stride**: Distance between consecutive row accesses
> - **128 total accessed**: Half of the 256-element region
> - **Contiguous column access**: Elements 8-15 in each row accessed sequentially
> - **Strided row access**: Rows accessed with 16-element stride


## Restriction on SBUF/PSUM Tensors

For SBUF/PSUM tensors, the first tuple must always be the access for the
partition dimension. On NeuronCore v2/v3/v4, the access on the partition
dimension must be continuous, meaning that the step of the leading dimension
must be the element count of the entire free dimension of the tensor.
Therefore, given a tensor of shape `(p_dim, f_dim0, fdim1)`, the step of
the leading dimension must be `f_dim0 * f_dim1`.

The following example is not allowed because it reads every other partition.


```python
t = nl.ndarray((16, 32), dtype=nl.float32, buffer=nl.sbuf)

# The following is illegal, because the first stride is 16*2 and reads every other partition
t.ap(pattern=[[64, 8], [1, 32]], offset=0)
```



> **Figure: memory access visualization 2**
>
> A memory access pattern visualization showing a 32x16 element grid with alternating accessed and unaccessed rows, demonstrating a strided row access pattern where every other row is accessed completely.
>
> This diagram visualizes a memory access pattern that accesses alternating rows in a larger memory region, common in certain tensor operations where data is interleaved or when accessing every other row.
>
> **Title and Layout:**
> - Title: "Memory Access Visualization"
> - Grid: 16 rows (labeled 0-15) x 32 columns (labeled 0-31)
> - Total grid size: 512 elements
>
> **Memory Access Pattern:**
> The grid displays an alternating row access pattern:
> - **Row 0**: Fully accessed (all 32 elements - green with white dots)
> - **Row 1**: Unaccessed (all 32 elements - white/empty)
> - **Row 2**: Fully accessed
> - **Row 3**: Unaccessed
> - **Row 4**: Fully accessed
> - **Row 5**: Unaccessed
> - **Row 6**: Fully accessed
> - **Row 7**: Unaccessed
> - **Row 8**: Fully accessed
> - **Row 9**: Unaccessed
> - **Row 10**: Fully accessed
> - **Row 11**: Unaccessed
> - **Row 12**: Fully accessed
> - **Row 13**: Unaccessed
> - **Row 14**: Fully accessed
> - **Row 15**: Unaccessed
>
> **Pattern Characteristics:**
> - Every even-numbered row (0, 2, 4, 6, 8, 10, 12, 14) is fully accessed
> - Every odd-numbered row (1, 3, 5, 7, 9, 11, 13, 15) is completely skipped
> - All 32 columns are accessed within each accessed row
> - Creates a "striped" pattern in the visualization
>
> **Legend (Bottom):**
> - Green circle with dot: "Accessed" - elements that are read/written
> - "N Overlapping" (orange): Indicates multiple access (not present in this pattern)
> - White square: "Unaccessed" - elements not accessed
>
> **Statistics (Bottom):**
> - Total elements: 256 (half of the 512-element region)
> - Index range: 0 - 479
> - Memory region: 32 x 16 = 512 elements
> - Out of bounds: 0 indices
>
> **Key Elements:**
> - **Alternating row pattern**: Every other row accessed
> - **32 columns per row**: Full row width accessed when row is active
> - **8 accessed rows**: Rows 0, 2, 4, 6, 8, 10, 12, 14
> - **8 skipped rows**: Rows 1, 3, 5, 7, 9, 11, 13, 15
> - **256 total accessed**: Half of total elements
> - **Row stride of 2**: Access jumps every 2 rows


## Restriction on Nested Indexing

The `.ap` method is only allowed on `nl.ndarray` and cannot be called on a
tile produced by it. For example, the following would result in an error.


```python
t = nl.ndarray((128, 256), dtype=nl.float32, buffer=nl.sbuf)
t.ap(pattern=[[256, 128],[1, 256]], offset=0).ap(pattern=[[256, 64], [1, 64]], offset=0)
     ^-- cannot specify an access pattern on an already indexed tensor
```


## Reinterpret Cast with `ap`

The `dtype` parameter can be used for reinterpret casting the tensor.
Since both the pattern and the offset are in terms of number of elements,
not bytes, the count must be computed accordingly. See the following example
of reinterpret cast from `INT32` to `BF16`.


```python
t = nl.ndarray((128, 256), dtype=nl.int32, buffer=nl.sbuf)
cast_to_bf16 = t.ap(pattern=[
  [512, 128], [1, 512]
 ], # notice the number of elements is doubled due to dtype size change
offset = 0) # cast_to_bf16 has shape (128, 512)
```