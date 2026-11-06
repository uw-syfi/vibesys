package k8s

import "fmt"

// The package's own pods (seed, probe, phase): their Job objects and the file exchange.
//
// A pod of the package runs the task image under a small shell wrapper:
//
//  1. it waits until CONTROL/ready exists; the caller first writes the input files into WorkDir
//     and that marker last, in one tar stream (KubeAPI.PutFiles);
//  2. it runs the command, which leaves its outputs in WorkDir and its output in the pod log, and
//     writes the exit status to CONTROL/exit;
//  3. it waits until CONTROL/collected exists, so the caller can copy WorkDir out of the running
//     container, then exits with the command's status.
//
// The inputs and outputs travel through the API server's exec stream, so no ConfigMap size limit
// applies, and the Job's activeDeadlineSeconds ends a pod nobody collects.
const (
	Root          = "/vse"
	WorkDir       = Root + "/work"
	ControlDir    = Root + "/control"
	readyMark     = "ready"
	exitMark      = "exit"
	collectedMark = "collected"
	// PodLabel is the role, on every pod of the package.
	PodLabel = "vibesys.dev/evaluator-pod"
)

// pathFixup makes loadgen findable in the pod however the task image installed vibesys-evaluator:
// on PATH, next to the vibesys-evaluator binary (the wheel keeps both in its _bin directory), or in
// the wheel's _bin directory itself. Directories are appended, so what is already on PATH wins.
const pathFixup = `for d in "$(dirname "$(readlink -f "$(command -v vibesys-evaluator 2>/dev/null)" 2>/dev/null)" 2>/dev/null)" "$(python -c 'import os, vibesys_evaluator; print(os.path.join(os.path.dirname(vibesys_evaluator.__file__), "_bin"))' 2>/dev/null)"; do
  if [ -x "$d/loadgen" ]; then PATH="$PATH:$d"; fi
done
export PATH
`

var wrapper = pathFixup + fmt.Sprintf(`set -u
mkdir -p %[1]s %[2]s
until [ -e %[2]s/%[3]s ]; do sleep 0.2; done
"$@"
echo $? > %[2]s/%[4]s.tmp && mv %[2]s/%[4]s.tmp %[2]s/%[4]s
until [ -e %[2]s/%[5]s ]; do sleep 0.2; done
exit "$(cat %[2]s/%[4]s)"
`, WorkDir, ControlDir, readyMark, exitMark, collectedMark)

// Role is what a pod of the package is for.
type Role string

// The roles of the package's pods.
const (
	RoleSeed  Role = "seed"
	RoleProbe Role = "probe"
	RolePhase Role = "phase"
)

// Size is a pod's CPU and memory (requests equal limits).
type Size struct{ CPU, Memory string }

// Sizes of the package's pods. A phase pod's size is fixed so the saturation guard has a known
// budget (spec: 6 CPUs, 4 GiB, what both yardsticks' load generators used).
var Sizes = map[Role]Size{
	RoleSeed:  {"4", "2Gi"},
	RoleProbe: {"500m", "256Mi"},
	RolePhase: {"6", "4Gi"},
}

// job is a one-pod Job of the package: never retried, no API credentials, no service links.
func job(name string, role Role, image string, argv []string, deadlineSecs int) Object {
	size := Sizes[role]
	resources := map[string]any{"cpu": size.CPU, "memory": size.Memory}
	labels := map[string]any{PodLabel: string(role)}
	command := []any{"sh", "-c", wrapper, "vse-pod"}
	for _, a := range argv {
		command = append(command, a)
	}
	return Object{
		"apiVersion": "batch/v1",
		"kind":       "Job",
		"metadata":   map[string]any{"name": name, "labels": labels},
		"spec": map[string]any{
			"backoffLimit":          0,
			"activeDeadlineSeconds": deadlineSecs,
			"template": map[string]any{
				"metadata": map[string]any{"labels": labels},
				"spec": map[string]any{
					"restartPolicy":                 "Never",
					"terminationGracePeriodSeconds": 5,
					"automountServiceAccountToken":  false,
					"enableServiceLinks":            false,
					"containers": []any{map[string]any{
						"name":            "main",
						"image":           image,
						"imagePullPolicy": "IfNotPresent",
						"command":         command,
						"resources":       map[string]any{"requests": resources, "limits": resources},
						"volumeMounts":    []any{map[string]any{"name": "vse", "mountPath": Root}},
					}},
					"volumes": []any{map[string]any{"name": "vse", "emptyDir": map[string]any{}}},
				},
			},
		},
	}
}
