# A frozen prediction

A prediction is written down before the change exists, so the measurement can show it right or
wrong. It goes in the plan or hypothesis it supports, as one block, and is never edited after a
worker starts; a revised prediction is a new block beside it, with its own date.

```
Prediction: <metric> <predicted change, %> against the head (head simulated <value>, head
  measured <value>, residual <+/-x%>)
Predicted value: <candidate simulated value>; only for kind match with residual within 10%,
  otherwise leave this line out
Kind: match | proxy   (proxy: what differs from the engine, one item each)
Simulator: <endpoint or $SERVINGSTUDIO_SIM>, revision <sim_commit or checkout HEAD>
Deployment: <preset id and parameters, or the preset file>, and the overrides that make the change
Workload: <the benchmark's: source, request count, arrivals or load, lengths, acceptance>
Evidence: <result directories or simulation ids; the command that made each>
Untrusted: <parts of the model this number rests on that the simulator does not model well, or
  "none known">
```

The predicted change is (candidate simulated - head simulated) / head simulated: both from the
same simulator, revision and workload. The head's measured value and the residual say how far
to trust it; never correct the candidate's value by the residual and present the result as
simulated. Label any such arithmetic as yours, beside the block, not in it.

A what-if bound is not a prediction and is not frozen as one. Write it as a line of its own,
`Bound: <metric> at most <value or change> if <what was scaled, by how much>`, beside the
plan's gain bound.

When the skill stopped at Setup or step 1, the plan has no Prediction block; one line says why
("no simulator: the catalog models this architecture only in FP8, the task runs BF16"; "no
simulator: launch overhead, which this idea removes, is not modeled").

How to read one when deciding:

- Rank plans by predicted change, and trust a small change less when the residual is large: a
  +8% change on a model 12% off the head is weak evidence, a +30% one is stronger. This is a
  judgment, not an error bar; a systematic residual does not bound the error of a change.
- A bound sets the plan's gain bound; it is not a predicted change.
- A proxy prediction ranks changes; its size carries the hardware difference too.
- Two predictions are comparable only when they name the same simulator revision.

After the candidate is measured, the outcome is written beside the prediction:

```
Outcome: measured <value> (<change, %> against the head); predicted <value>; miss <+/-x%>
Miss explained by: <the model (which part), the implementation (it did less than planned), the
  measurement (noise, another workload), or "not explained">
```
