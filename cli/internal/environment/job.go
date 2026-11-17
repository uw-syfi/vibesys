package environment

import (
	"maps"
	"slices"
	"strconv"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// maxTimeoutSeconds is the largest limit the job environment states (its rule): a longer limit is
// stated as this one, since the variable is for a command to size a margin by, not to enforce.
const maxTimeoutSeconds = 31536000

// TimeoutSeconds is a limit in whole seconds, rounded up, as the job environment states it. A limit
// that is not positive is zero, which states none.
func TimeoutSeconds(limit time.Duration) uint32 {
	if limit <= 0 {
		return 0
	}
	seconds := (limit + time.Second - 1) / time.Second
	return uint32(min(seconds, maxTimeoutSeconds))
}

// NoteGPUs says in the job environment which GPUs of a pool the job was given, in the words every
// vendor shares (VIBESYS_GPUS, VIBESYS_GPU_DEVICES), so that a command can read how many it has and
// which. A job given none leaves both unset.
func NoteGPUs(job *cliv1.JobEnvironment, grant *core.Grant) {
	if grant == nil {
		return
	}
	job.Gpus = uint32(grant.Devices.Len())
	numbers := make([]string, 0, grant.Devices.Len())
	for _, d := range grant.Devices.Devices() {
		numbers = append(numbers, strconv.Itoa(int(d)))
	}
	job.GpuDevices = strings.Join(numbers, ",")
}

// JobVariables are the variables a command of a job is started with, for the job environment the
// environment states. It fails when the message breaks its rules, which names the variable.
func JobVariables(job *cliv1.JobEnvironment) (envcodec.Environment, error) {
	return envcodec.Encode(job)
}

// Overlay is base, the environment of the process that starts a command, with the variables of e
// set and the ones e leaves unset removed: a command is told only what its job is, never what an
// enclosing job's environment said.
func Overlay(base []string, e envcodec.Environment) []string {
	unset := make(map[string]bool, len(e.Unset)+len(e.Set))
	for _, name := range e.Unset {
		unset[name] = true
	}
	for name := range e.Set {
		unset[name] = true
	}
	out := make([]string, 0, len(base)+len(e.Set))
	for _, entry := range base {
		if name, _, _ := strings.Cut(entry, "="); !unset[name] {
			out = append(out, entry)
		}
	}
	for _, name := range slices.Sorted(maps.Keys(e.Set)) {
		out = append(out, name+"="+e.Set[name])
	}
	return out
}
