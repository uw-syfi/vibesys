# NCC_EHCA005

**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`

NCC_EHCA005
**Error message**: The compiler encountered a custom call instruction with a target name that is not recognized.

The Neuron compiler currently recognizes the following custom call targets:

* AwsNeuronErf

* AwsNeuronGelu

* AwsNeuronGeluApprxTanh

* AwsNeuronGeluBackward

* AwsNeuronSilu

* AwsNeuronSiluBackward

* AwsNeuronRmsNorm

* AwsNeuronSoftmax

* AwsNeuronSoftmaxBackward

* AwsNeuronCollectiveMatmul

* AwsNeuronIntMatmult

* AwsNeuronArgMax

* AwsNeuronArgMin

* AwsNeuronTopK

* AwsNeuronDropoutMaskV1

* AwsNeuronCustomNativeKernel

* AwsNeuronCustomOp

* AwsNeuronDevicePrint

* ResizeNearest

* ResizeBilinear

* ResizeNearestGrad

* AwsNeuronLNCShardingConstraint

* AwsNeuronTransferWithStaticRing

* AwsNeuronModuleMarkerStart-Forward

* AwsNeuronModuleMarkerStart-Backward

* AwsNeuronModuleMarkerEnd-Forward

* AwsNeuronModuleMarkerEnd-Backward

* NeuronBoundaryMarker-Start

* NeuronBoundaryMarker-End

Erroneous code example:


```python
def lowering(ctx, x_val):
    result_type = ir.RankedTensorType(x_val.type)
    # This target name will not be recognized by HandleCustomCall
    return hlo.CustomCallOp(
        [result_type],
        [x_val],
        call_target_name="UNRECOGNIZED_TARGET",
        has_side_effect=ir.BoolAttr.get(False),
    ).results
```


Use a supported custom call target:


```python
def lowering(ctx, x_val):
    result_type = ir.RankedTensorType(x_val.type)
    return hlo.CustomCallOp(
        [result_type],
        [x_val],
        call_target_name="AwsNeuronSilu",
        has_side_effect=ir.BoolAttr.get(False),
        backend_config=ir.StringAttr.get(""),
        api_version=ir.IntegerAttr.get(ir.IntegerType.get_signless(32), 2),
    ).results
```


**This document is relevant for**: `Inf1`, `Inf2`, `Trn1`, `Trn2`, `Trn3`