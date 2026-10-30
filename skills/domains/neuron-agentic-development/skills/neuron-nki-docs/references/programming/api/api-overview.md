# NKI Library Kernel API Reference

NKI Library Kernel API Reference
The NKI Library provides pre-built reference kernels you can use directly in your model development with the AWS Neuron SDK and NKI. These kernel APIs provide the default classes, functions, and parameters you can use to integrate the NKI Library kernels into your models.

**Source code for these kernel APIs can be found at**: [aws-neuron/nki-library](https://github.com/aws-neuron/nki-library)

## Normalization and Quantization Kernels


| [ RMSNorm-Quant Kernel API Reference ](../../reference/library/rmsnorm-quant.md) | API reference for the RMSNorm-Quant kernel included in the NKI Library. The kernel performs optional RMS normalization followed by quantization to fp8 . |
| --- | --- |


## QKV Projection Kernels


| [ QKV Kernel API Reference ](../../reference/library/qkv.md) | API reference for the QKV kernel included in the NKI Library. The kernel performs Query-Key-Value projection with optional normalization fusion. |
| --- | --- |


## Attention Kernels


| [ Attention CTE Kernel API Reference ](../../reference/library/attention-cte.md) | API reference for the Attention CTE kernel included in the NKI Library. The kernel implements attention specifically optimized for Context Encoding use cases. |
| --- | --- |
| [ Attention TKG Kernel API Reference ](../../reference/library/attention-tkg.md) | API reference for the Attention TKG kernel included in the NKI Library. The kernel implements attention specifically optimized for Token Generation (Decoding) use cases with small active sequence lengths. |


## Multi-Layer Perceptron (MLP) Kernels


| [ MLP Kernel API Reference ](../../reference/library/mlp.md) | API reference for the MLP kernel included in the NKI Library. The kernel implements a Multi-Layer Perceptron with optional normalization fusion and various optimizations. |
| --- | --- |


## Output Projection Kernels


| [ Output Projection CTE Kernel API Reference ](../../reference/library/output-projection-cte.md) | API reference for the Output Projection CTE kernel included in the NKI Library. The kernel computes the output projection operation optimized for Context Encoding use cases. |
| --- | --- |
| [ Output Projection TKG Kernel API Reference ](../../reference/library/output-projection-tkg.md) | API reference for the Output Projection TKG kernel included in the NKI Library. The kernel computes the output projection operation optimized for Token Generation use cases. |