---
name: setup
description: "Use when a session is about to make its first vibesys-cli call (vibesys:create-task and vibesys:optimize start here), when vibesys-cli is missing, fails or has a version the skills do not support, when the user asks to install, upgrade or check it, when they ask to configure the machine for it (its GPUs, a cluster, the placement file), or when a task or job is refused for a missing GPU or for --gpus. Says which reference to read for each."
---

# Set up

You prepare the environment the vibesys skills run in. Read only the reference you need.

| You need | Read |
| --- | --- |
| The `vibesys-cli` binary: find, install, upgrade, check its version, export `VIBESYS_CLI`. Every session starts here. | [cli.md](references/cli.md) |
| The machine's GPUs or a cluster: the placement file (`cli.toml`), and what to do when a task or job is refused for a missing GPU or for `--gpus`. | [placements.md](references/placements.md) |

Rules for both:

- Never edit shell profiles.
- Install, upgrade, or write a config file only with the user's approval. The placement file
  belongs to the user: never change it to get past a refusal while optimizing.
- Creating a task is `vibesys:create-task`, not this skill.
