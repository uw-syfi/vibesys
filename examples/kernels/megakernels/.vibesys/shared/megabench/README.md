# MegaBench evaluator (vendored)

The trusted part of the MegaBench megakernel benchmark, trimmed to the modules that the tasks
of this example import: the case definitions (`cases.py`), the harness that checks and times
a submission (`harness/`), the VibeSys adapter (`integrations/vibesys_case_evaluator.py`) and
the input generators and PyTorch references of the workloads (`tasks/`). The files are
unmodified copies, so `cases.py` also lists cases whose task modules are not included here,
and the command-line and container entry points of `harness/runner.py` are not usable from
this copy. Only [`../evaluate.py`](../evaluate.py) is supported.
