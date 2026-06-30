# Neuron Compiler Error Codes

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

Neuron Compiler Error Codes
This page lists the error codes you can encounter while developing with the Neuron Compiler. For more details on any individual error, click the link for that error code in the table below.


| Error Code | Error Message | Recommendation |
| --- | --- | --- |
| [ NCC_EARG001 ](error-codes/EARG001.md#error-code-earg001) | Unsupported Logical Neuron Core (LNC) configuration. | You attempted to use a Logical Neuron Core configuration that is not supported by the target Neuron architecture. |
| [ NCC_EBVF030 ](error-codes/EBVF030.md#error-code-ebvf030) | The number of instructions generated exceeds the limit. | Consider applying model parallelism as partitioning the model will help break large computational graphs into smaller subgraphs. |
| [ NCC_EHCA005 ](error-codes/EHCA005.md#error-code-ehca005) | The compiler encountered a custom call instruction with a target name that is not recognized. | Use a supported custom call target from the list of recognized targets. |
| [ NCC_EOOM001 ](error-codes/EOOM001.md#error-code-eoom001) | The combined memory needed for the model’s activation tensors exceeds the high-bandwidth memory limit. | You may need to reduce batch/tensor size or utilize pipeline/tensor parallelism via neuronx-distributed. |
| [ NCC_EOOM002 ](error-codes/EOOM002.md#error-code-eoom002) | The combined memory needed for the model’s activation tensors exceeds the high-bandwidth memory limit. | You may need to reduce batch/tensor size or utilize pipeline/tensor parallelism via neuronx-distributed. |
| [ NCC_ESFH002 ](error-codes/ESFH002.md#error-code-esfh002) | The compiler encountered a unsigned 64-bit integer constant with a value that cannot be safely converted to 32-bit representation. | Try to use uint32 for constants when possible and restructure code to avoid large constants. |
| [ NCC_ESPP004 ](error-codes/ESPP004.md#error-code-espp004) | The compiler encountered a data type that is not supported for code generation. | Use a supported data type as listed in the Neuron documentation. |
| [ NCC_ESPP047 ](error-codes/ESPP047.md#error-code-espp047) | Unsupported 8-bit floating-point data type. | The compiler found usage of an unsupported 8-bit floating-point data type. Convert to a supported type like torch.float16. |
| [ NCC_EUOC002 ](error-codes/EUOC002.md#error-code-euoc002) | An unsupported operator was used. | Try using alternative operators from the full list of supported operators via neuronx-cc list-operators –framework XLA to workaround the limitation. |
| [ NCC_EVRF001 ](error-codes/EVRF001.md#error-code-evrf001) | An unsupported operator was used. | Try using alternative operators from the full list of supported operators to workaround the limitation. |
| [ NCC_EVRF004 ](error-codes/EVRF004.md#error-code-evrf004) | Complex data types are not supported on the Neuron device. | You cannot use complex data types (such as complex64 , complex128 , and others) on the Neuron device directly. |
| [ NCC_EVRF005 ](error-codes/EVRF005.md#error-code-evrf005) | Unsupported F8E4M3FNUZ, F8E4M3B11FNUZ, or F8E5M2FNUZ data type. | The compiler found usage of unsupported 8-bit floating-point data types. Convert to a supported type like torch.float16. |
| [ NCC_EVRF006 ](error-codes/EVRF006.md#error-code-evrf006) | The compiler encountered a RNGBitGenerator operation using a random number generation algorithm other than RNG_DEFAULT. | Ensure that you are using standard JAX/PyTorch random APIs and not explicity specifying an RNG algorithm. |
| [ NCC_EVRF007 ](error-codes/EVRF007.md#error-code-evrf007) | The number of instructions generated exceeds the limit. | Consider applying model parallelism as partitioning the model will help break large computational graphs into smaller subgraphs. |
| [ NCC_EVRF009 ](error-codes/EVRF009.md#error-code-evrf009) | The combined memory needed for the model’s activation tensors exceeds the high-bandwidth memory limit. | You may need to reduce batch/tensor size or utilize pipeline/tensor parallelism via neuronx-distributed. |
| [ NCC_EVRF010 ](error-codes/EVRF010.md#error-code-evrf010) | The compiler encountered simultaneous use of input and kernel dilation, which is not supported. | If possible, use only input or kernel dilation, not both simultaneously. |
| [ NCC_EVRF011 ](error-codes/EVRF011.md#error-code-evrf011) | The compiler encountered strided convolution combined with dilated input, which is not supported. | If possible, remove stride or input dilation, or apply upsampling and downsampling separately. |
| [ NCC_EVRF013 ](error-codes/EVRF013.md#error-code-evrf013) | TopK does not support integer input tensors (int32, int64). | The TopK operation cannot be performed on integer data types. |
| [ NCC_EVRF015 ](error-codes/EVRF015.md#error-code-evrf015) | The compiler encountered a custom call instruction with a target name that is not recognized. | Use a supported custom call target from the list of recognized targets. |
| [ NCC_EVRF016 ](error-codes/EVRF016.md#error-code-evr016) | The scatter-reduce operation cannot perform reduction logic if the data being scattered or the destination tensor is using an integer or boolean data type. | Cast your input and source tensors to a floating-point data type (e.g., torch.float32 or torch.bfloat16). |
| [ NCC_EVRF017 ](error-codes/EVRF017.md#error-code-evrf017) | Reduce-window operation with base dilation greater than 1 is not supported. | Change base dilation to be all 1s or consider manual dilation if necessary. |
| [ NCC_EVRF018 ](error-codes/EVRF018.md#error-code-evrf018) | Reduce-window operation with window dilation greater than 1 is not supported. | Remove window_dilation or change values to be all 1s, or consider manual dilation if necessary. |
| [ NCC_EVRF019 ](error-codes/EVRF019.md#error-code-evrf019) | The compiler encountered a reduce-window operation with more or less than 2 operands. | If possible, split multi-operand reduce_window with multiple single-operand reduce_window operations. |
| [ NCC_EVRF022 ](error-codes/EVRF022.md#error-code-evrf022) | Shift-right-arithmetic operation on non 32-bit inputs is not supported. Cast the first argument’s data type to be S32, U32, or F32. | You need to use 32-bit data types for shift operations. Cast inputs to int32, uint32, or float32. |
| [ NCC_EVRF024 ](error-codes/EVRF024.md#error-code-evrf024) | The output tensor size limit of 4GB was exceeded. | Reduce batch/tensor size or utilize tensor parallelism via neuronx-distributed. |
| [ NCC_EVRF031 ](error-codes/EVRF031.md#error-code-evrf031) | The compiler encountered a scatter out-of-bounds error. | Ensure that the iota size matches the operand dimension size. |
| [ NCC_EXSP001 ](error-codes/EXSP001.md#error-code-exsp001) | The combined memory needed for the model’s activation tensors exceeds the high-bandwidth memory limit. | You may need to reduce batch/tensor size or utilize pipeline/tensor parallelism via neuronx-distributed. |
| [ NCC_EXTP004 ](error-codes/EXTP004.md#error-code-extp004) | The number of instructions generated exceeds the limit. | Consider applying model parallelism as partitioning the model will help break large computational graphs into smaller subgraphs. |


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`