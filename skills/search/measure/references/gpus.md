# GPUs: the task's budget, and your jobs

A task is measured on a stated amount of hardware: its GPU budget (for example, 2 H100s). The
machine can have more GPUs than that. `vibesys-cli` does not enforce the budget on a measure,
which takes every GPU of the node, so staying inside it is your job.

## The budget

- Where it is: `vibesys-cli task card <task>` prints the hardware the task declares (GPU model and
  count), and `OBJECTIVE.md` states the rules. If the task states no budget, say so in your plan or
  handoff and state which budget you assumed and why. Do not take the whole machine as the budget.
- Why it matters: a number taken on more GPUs than the budget cannot be compared with the baseline.
  Using extra GPUs is not an optimization, because it changes what is measured. Do not propose it,
  build it or report it as a gain.

## Using the budget well

Inside the budget, a candidate that leaves GPUs idle leaves speed unused. A server often starts on
one GPU unless it is told to use more, and its benchmark number still looks fine.

- **Count what is used.** Read the launch command and config on the commit: the parallel degrees
  (tensor, data, expert), the replica count, and `CUDA_VISIBLE_DEVICES`. Compare the devices they
  use with the budget. Budgeted devices left idle are headroom, when the task's rules allow the
  parallelism.
- **Check under load.** Do not trust the count alone. In the same job that drives the load, sample
  per-device use every second or so: `nvidia-smi --query-gpu=index,utilization.gpu,memory.used
  --format=csv -l 1` (`rocm-smi --showuse` on AMD). A device near 0% for the whole run was idle.
- **Report.** Say how many devices were busy, out of the budget. Idle budgeted devices are a
  finding. If your change used devices beyond the budget, report that as a violation, not a gain.

## Your own jobs

- A check or command of a GPU task says how many GPUs it takes with `--gpus N`, where the operator
  configured a pool of GPUs. There is no default. Ask for the fewest the step needs (the weights
  and cache fit in memory, and the parallel degree the launch needs), because fewer GPUs per job
  lets more jobs run at once. Run `vibesys-cli jobs` first and read the counts earlier jobs asked
  for and the devices they ran on. A refusal names the sizes the placement allows.
- A measure takes the whole node and refuses `--gpus`. The server it measures must still stay
  inside the budget.
