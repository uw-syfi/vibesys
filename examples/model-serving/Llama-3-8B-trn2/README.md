Llama-3-8B input bundle — AWS Trainium (trn2) target.

This is the `examples/model-serving/Llama-3-8B` bundle retargeted from H100 to a single
Trainium2 device. The model and accuracy checker are shared; this bundle's
Request Factory benchmark sweeps Trainium-specific serving capacity. The
deployment target is described in `OBJECTIVE.md` (from-scratch model on a
NeuronCore via the AWS Neuron SDK, BF16, `/dev/neuron0`).

Use:

```bash
vibesys --runs-dir /work/vibesys-runs --local \
  --input examples/model-serving/Llama-3-8B-trn2 \
  --backend trainium
```

The task runs on the native TorchNeuron DLC (`torch-neuronx` 2.14, Neuron SDK
2.33). Its `Dockerfile` moves TorchNeuron's editable install out of
`/workspace`, where VibeSys mounts the candidate; copy it into any new trainium
task. The `neuron-explorer` profiler is selected automatically.

The model weights (`meta-llama/Llama-3.1-8B-Instruct`) are downloaded from the
`model_id` in `reference/meta.json` on first run.
