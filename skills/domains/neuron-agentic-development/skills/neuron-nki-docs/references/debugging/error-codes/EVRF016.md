# NCC_EVRF016

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EVRF016
The NCC_EVRF016 error is raised when the Neuron compiler detects that you are trying to use an integer or boolean type with one of the restricted reduction functions.

**Error message**: The scatter-reduce operation cannot perform reduction logic if the data being scattered or the destination tensor is using an integer or boolean data type.

The hardware instructions used on the Neuron device for these specific scatter-and-reduce functions are optimized for and limited to floating-point arithmetic. When the compiler detects that you are trying to use an integer or boolean type with one of the restricted reduction functions, it stops the compilation process to prevent a hardware crash or incorrect calculation.

**Example of the error**

The following example shows the **NCC_EVRF016** error because the `input_tensor` is defined using an integer data type (`torch.int32`) while being used with a reduction function (`reduce='sum'`) in the `scatter_reduce_` operation.


```python
def forward(self, input_tensor, indices_tensor, src_tensor):
    output = input_tensor.clone()

    output.scatter_reduce_(
        dim=1,
        index=indices_tensor,
        src=src_tensor,
        reduce='sum',
    )
    return output

# ERROR: using integer dtype with scatter-reduce
input_tensor = torch.zeros(BATCH_SIZE, DIM_SIZE, dtype=torch.int32)
...
```


**How to fix**

To fix this error, you must cast your input and source tensors to a floating-point data type (e.g., torch.float32 or torch.bfloat16).


```python
def forward(self, input_tensor, indices_tensor, src_tensor):
    output = input_tensor.clone()

    output.scatter_reduce_(
        dim=1,
        index=indices_tensor,
        src=src_tensor,
        reduce='sum',
    )
    return output

# FIXED: changed to float32
# now works with scatter-reduce
input_tensor = torch.zeros(BATCH_SIZE, DIM_SIZE, dtype=torch.float32)
...
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`