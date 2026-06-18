Llama-3-8B input bundle — AWS Trainium (trn2) target.

This is the `examples/Llama-3-8B` bundle retargeted from H100 to a single
Trainium2 device. Same model, reference, accuracy checker, and benchmark; the
difference is the deployment target described in `OBJECTIVE.md` (from-scratch
model on a NeuronCore via the AWS Neuron SDK, BF16, `/dev/neuron0`).

Use:
- `--ref examples/Llama-3-8B-trn2/reference`
- `--acc-checker examples/Llama-3-8B-trn2/accuracy_checker`
- `--bench examples/Llama-3-8B-trn2/benchmark`
- `--backend trainium --docker` (Neuron DLC container; profiler is
  `neuron-explorer`, selected automatically)

The model weights (`meta-llama/Llama-3.1-8B-Instruct`) are downloaded from the
`model_id` in `reference/meta.json` on first run.
