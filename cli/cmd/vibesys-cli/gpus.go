package main

import (
	"fmt"
	"slices"
	"strconv"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Text about the GPUs of jobs. These functions name; the JSON path never calls them.

// deviceList writes device numbers in ascending order as ranges, such as "0-3,6".
func deviceList(devices []uint32) string {
	var parts []string
	for i := 0; i < len(devices); {
		j := i
		for j+1 < len(devices) && devices[j+1] == devices[j]+1 {
			j++
		}
		if j > i {
			parts = append(parts, fmt.Sprintf("%d-%d", devices[i], devices[j]))
		} else {
			parts = append(parts, strconv.FormatUint(uint64(devices[i]), 10))
		}
		i = j + 1
	}
	return strings.Join(parts, ",")
}

// gpusWord says a number of GPUs: "1 GPU", "4 GPUs".
func gpusWord(n uint32) string {
	if n == 1 {
		return "1 GPU"
	}
	return fmt.Sprintf("%d GPUs", n)
}

// describeGrant says the devices a job was given and who chose them: "devices 0,1", with
// "(chosen by the CLI)" or "(chosen by the cluster)" after it, and without the devices for a
// cluster's grant the CLI has not read back.
func describeGrant(g *cliv1.GpuGrant) string {
	who := "the CLI"
	if g.GetSource() == cliv1.GrantSource_GRANT_SOURCE_SLURM {
		who = "the cluster"
	}
	if len(g.GetDevices()) == 0 {
		return "(chosen by " + who + ")"
	}
	return fmt.Sprintf("devices %s (chosen by %s)", deviceList(g.GetDevices()), who)
}

// needsGPUs says whether the job's task needs the GPUs of a placement.
func needsGPUs(needs []cliv1.ResourceKind) bool {
	return slices.Contains(needs, cliv1.ResourceKind_RESOURCE_KIND_GPU) || slices.Contains(needs, cliv1.ResourceKind_RESOURCE_KIND_ROCM)
}

// describeJobGPUs says what a job asked of the GPUs and was given, or nothing for a job that has
// nothing to say of them: "gpus 2, devices 0,1 (chosen by the CLI)"; "gpus 8 (whole node)" for a
// measure, which takes the whole pool; "gpus 2" for a job that asked and has not started. A job
// of a task that needs GPUs whose record has no count and no grant, as one saved before they were
// recorded, says "gpus unknown" and never a guess.
func describeJobGPUs(j *cliv1.Job) string {
	grant := j.GetGpus()
	switch {
	case grant != nil && grant.GetWhole():
		return fmt.Sprintf("gpus %d (whole node), %s", grant.GetPoolSize(), describeGrant(grant))
	case grant != nil && len(grant.GetDevices()) > 0:
		return fmt.Sprintf("gpus %d, %s", len(grant.GetDevices()), describeGrant(grant))
	case j.GetSpec() != nil && j.GetSpec().Gpus != nil:
		return fmt.Sprintf("gpus %d", j.GetSpec().GetGpus())
	case grant != nil:
		return "gpus " + describeGrant(grant)
	case needsGPUs(j.GetNeeds()) && j.GetSpec().GetMeasure() == nil:
		return "gpus unknown"
	}
	return ""
}

// gpuFacts are the facts of an environment that say its machine has GPUs (the vendors' models).
var gpuFacts = []string{"gpu_model", "amd_gpu_model"}

// describeResultGPUs is describeJobGPUs for a saved result, which says the same of its job from
// what the daemon read of the job's record (use, nil when it said nothing). A result carries no
// needs, so one the record says nothing of says "gpus unknown" only when the environment it ran in
// pinned the model of a GPU, and nothing otherwise.
func describeResultGPUs(r *cliv1.Result, use *cliv1.JobGpus) string {
	grant := use.GetGrant()
	switch {
	case grant != nil && grant.GetWhole():
		return fmt.Sprintf("gpus %d (whole node), %s", grant.GetPoolSize(), describeGrant(grant))
	case grant != nil && len(grant.GetDevices()) > 0:
		return fmt.Sprintf("gpus %d, %s", len(grant.GetDevices()), describeGrant(grant))
	case use != nil && use.Asked != nil:
		return fmt.Sprintf("gpus %d", use.GetAsked())
	case grant != nil:
		return "gpus " + describeGrant(grant)
	case slices.ContainsFunc(gpuFacts, func(f string) bool { return r.GetEnvironment().GetFacts()[f] != "" }):
		return "gpus unknown"
	}
	return ""
}

// describeGPUHolders names the jobs that hold devices of a pool and which: "j41 (check, run r7) on
// devices 0-1, j42 (command) on devices 4-7", with "(whole node)" for a job that took the pool whole.
func describeGPUHolders(holders []*cliv1.GpuHolder) string {
	names := make([]string, len(holders))
	for i, h := range holders {
		name := describeHolder(&cliv1.WaitHolder{JobId: h.GetJobId(), Kind: h.GetKind(), RunId: h.GetRunId(), Gpus: h.GetGpus()}, "")
		if h.GetGpus().GetWhole() {
			name += " (whole node)"
		}
		names[i] = name
	}
	return strings.Join(names, ", ")
}

// describeGPUWaiters names the jobs that wait for devices of a pool and how many each wants:
// "j43 (4 GPUs), j44 (1 GPU)".
func describeGPUWaiters(waiting []*cliv1.GpuWaiter) string {
	names := make([]string, len(waiting))
	for i, w := range waiting {
		names[i] = fmt.Sprintf("%s (%s)", w.GetJobId(), gpusWord(w.GetWanted()))
	}
	return strings.Join(names, ", ")
}
