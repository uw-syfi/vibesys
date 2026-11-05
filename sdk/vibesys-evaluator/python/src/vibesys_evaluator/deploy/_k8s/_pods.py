"""The package's own pods (seed, probe, phase): their Job objects and the file exchange. Pure.

A pod of the package runs the task image under a small shell wrapper:

1. it waits until `CONTROL/ready` exists; the orchestrator first writes the input files
   into `IN_DIR` and that marker last, in one tar stream (`KubeAPI.put_files`);
2. it runs the command, its output going to the pod log, and writes the exit status to
   `CONTROL/exit`;
3. it waits until `CONTROL/collected` exists, so the orchestrator can copy `OUT_DIR`
   out of the running container, then exits with the command's status.

The inputs and outputs travel through the API server's exec stream, so no ConfigMap
size limit applies, and the Job's activeDeadlineSeconds ends a pod nobody collects.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

ROOT = "/vse"
IN_DIR = f"{ROOT}/in"
OUT_DIR = f"{ROOT}/out"
CONTROL = f"{ROOT}/control"
READY_MARK = "ready"
EXIT_MARK = "exit"
COLLECTED_MARK = "collected"
POD_LABEL = "vibesys.dev/evaluator-pod"  # the role, on every pod of the package

WRAPPER = f"""\
set -u
mkdir -p {IN_DIR} {OUT_DIR} {CONTROL}
until [ -e {CONTROL}/{READY_MARK} ]; do sleep 0.2; done
"$@"
echo $? > {CONTROL}/{EXIT_MARK}.tmp && mv {CONTROL}/{EXIT_MARK}.tmp {CONTROL}/{EXIT_MARK}
until [ -e {CONTROL}/{COLLECTED_MARK} ]; do sleep 0.2; done
exit "$(cat {CONTROL}/{EXIT_MARK})"
"""


class Role(StrEnum):
    SEED = "seed"
    PROBE = "probe"
    PHASE = "phase"


@dataclass(frozen=True)
class Size:
    cpu: str
    memory: str


# requests == limits. A phase pod's size is fixed so the saturation guard has a known
# budget (spec: 6 CPUs, 4 GiB, what both yardsticks' load generators used).
SIZES: Mapping[Role, Size] = {
    Role.SEED: Size(cpu="4", memory="2Gi"),
    Role.PROBE: Size(cpu="500m", memory="256Mi"),
    Role.PHASE: Size(cpu="6", memory="4Gi"),
}


def job(
    *, name: str, role: Role, image: str, argv: Sequence[str], deadline_secs: int
) -> dict[str, Any]:
    """A one-pod Job of the package: never retried, no API credentials, no service links."""
    size = SIZES[role]
    resources = {"cpu": size.cpu, "memory": size.memory}
    labels = {POD_LABEL: str(role)}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": deadline_secs,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "terminationGracePeriodSeconds": 5,
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "containers": [
                        {
                            "name": "main",
                            "image": image,
                            "imagePullPolicy": "IfNotPresent",
                            "command": ["sh", "-c", WRAPPER, "vse-pod", *argv],
                            "resources": {"requests": resources, "limits": resources},
                            "volumeMounts": [{"name": "vse", "mountPath": ROOT}],
                        }
                    ],
                    "volumes": [{"name": "vse", "emptyDir": {}}],
                },
            },
        },
    }
