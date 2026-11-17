package main

import (
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// parseDeviceList reads what deviceList writes: numbers and ranges separated by commas.
func parseDeviceList(t *testing.T, s string) []uint32 {
	t.Helper()
	var out []uint32
	for _, part := range strings.Split(s, ",") {
		lo, hi, isRange := strings.Cut(part, "-")
		a, err := strconv.Atoi(lo)
		if err != nil {
			t.Fatalf("%q: %v", s, err)
		}
		b := a
		if isRange {
			if b, err = strconv.Atoi(hi); err != nil || b <= a {
				t.Fatalf("%q: bad range %q", s, part)
			}
		}
		for d := a; d <= b; d++ {
			out = append(out, uint32(d))
		}
	}
	return out
}

// A list of devices is written so that reading it back gives the same devices, however they fall.
func TestADeviceListReadsBackAsTheSameDevices(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 5))
	for range 2000 {
		var devices []uint32
		density := r.Float64()
		for d := range uint32(64) {
			if r.Float64() < density {
				devices = append(devices, d)
			}
		}
		if len(devices) == 0 {
			continue
		}
		got := parseDeviceList(t, deviceList(devices))
		if len(got) != len(devices) {
			t.Fatalf("%v written as %q reads back as %v", devices, deviceList(devices), got)
		}
		for i := range got {
			if got[i] != devices[i] {
				t.Fatalf("%v written as %q reads back as %v", devices, deviceList(devices), got)
			}
		}
	}
	if got := deviceList([]uint32{0, 1, 2, 3, 6}); got != "0-3,6" {
		t.Errorf("deviceList = %q, want 0-3,6", got)
	}
}

func u32(n uint32) *uint32 { return &n }

func TestAJobSaysWhatItAskedOfTheGPUsAndWhereItRan(t *testing.T) {
	gpuTask := []cliv1.ResourceKind{cliv1.ResourceKind_RESOURCE_KIND_GPU}
	check := &cliv1.JobSpec{Settings: &cliv1.JobSpec_Check{Check: &cliv1.CheckSettings{}}}
	measure := &cliv1.JobSpec{Settings: &cliv1.JobSpec_Measure{Measure: &cliv1.MeasureSettings{}}}
	for want, j := range map[string]*cliv1.Job{
		"gpus 2, devices 0-1 (chosen by the CLI)": {
			Spec: &cliv1.JobSpec{Gpus: u32(2), Settings: check.Settings}, Needs: gpuTask,
			Gpus: &cliv1.GpuGrant{Pool: "gpu:local", PoolSize: 8, Devices: []uint32{0, 1}, Source: cliv1.GrantSource_GRANT_SOURCE_CLI},
		},
		"gpus 8 (whole node), devices 0-7 (chosen by the CLI)": {
			Spec: measure, Needs: gpuTask,
			Gpus: &cliv1.GpuGrant{Pool: "gpu:local", PoolSize: 8, Devices: []uint32{0, 1, 2, 3, 4, 5, 6, 7}, Whole: true, Source: cliv1.GrantSource_GRANT_SOURCE_CLI},
		},
		"gpus 4, devices 4,6,8,10 (chosen by the cluster)": {
			Spec: check, Needs: gpuTask,
			Gpus: &cliv1.GpuGrant{Pool: "gpu:n1", PoolSize: 16, Devices: []uint32{4, 6, 8, 10}, Source: cliv1.GrantSource_GRANT_SOURCE_SLURM},
		},
		"gpus 4":       {Spec: &cliv1.JobSpec{Gpus: u32(4), Settings: check.Settings}, Needs: gpuTask},
		"gpus unknown": {Spec: check, Needs: gpuTask},
		"":             {Spec: check},
		// A queued measure takes the whole pool, and says nothing of a count.
		" ": {Spec: measure, Needs: gpuTask},
	} {
		if got := describeJobGPUs(j); got != strings.TrimSpace(want) {
			t.Errorf("%v: %q, want %q", j, got, want)
		}
	}
}

func TestAResultSaysItsGPUsOrThatTheyAreUnknown(t *testing.T) {
	facts := &cliv1.Result{Environment: &cliv1.EnvironmentFingerprint{Facts: map[string]string{"gpu_model": "h100"}}}
	bare := &cliv1.Result{Environment: &cliv1.EnvironmentFingerprint{}}
	grant := &cliv1.GpuGrant{Pool: "gpu:local", PoolSize: 8, Devices: []uint32{2, 3}, Source: cliv1.GrantSource_GRANT_SOURCE_CLI}
	for want, c := range map[string]struct {
		r   *cliv1.Result
		use *cliv1.JobGpus
	}{
		"gpus 2, devices 2-3 (chosen by the CLI)": {facts, &cliv1.JobGpus{Asked: u32(2), Grant: grant}},
		"gpus 2":       {facts, &cliv1.JobGpus{Asked: u32(2)}},
		"gpus unknown": {facts, nil},
		"":             {bare, nil},
	} {
		if got := describeResultGPUs(c.r, c.use); got != want {
			t.Errorf("%v %v: %q, want %q", c.r, c.use, got, want)
		}
	}
}

func TestAWaitForGPUsSaysHowManyItWantsHowManyAreFreeAndWhoHoldsTheRest(t *testing.T) {
	holder := func(id string, devices ...uint32) *cliv1.WaitHolder {
		return &cliv1.WaitHolder{JobId: id, Task: "t", Kind: cliv1.JobKind_JOB_KIND_CHECK, Gpus: &cliv1.GpuGrant{Pool: "gpu:local", PoolSize: 8, Devices: devices, Source: cliv1.GrantSource_GRANT_SOURCE_CLI}}
	}
	for want, w := range map[string]*cliv1.WaitReason{
		"waiting for 4 GPUs of gpu:local, 2 free, held by j41 (check) on devices 0-2, j42 (check) on devices 4-6": {
			Kind: cliv1.WaitKind_WAIT_KIND_FULL, Resource: "gpu:local", GpusWanted: 4, GpusFree: 2,
			Holders: []*cliv1.WaitHolder{holder("j41", 0, 1, 2), holder("j42", 4, 5, 6)},
		},
		"waiting for 1 GPU of gpu:local, 2 free, behind j43 (check), which waits for it": {
			Kind: cliv1.WaitKind_WAIT_KIND_BEHIND, Resource: "gpu:local", GpusWanted: 1, GpusFree: 2,
			Holders: []*cliv1.WaitHolder{{JobId: "j43", Task: "t", Kind: cliv1.JobKind_JOB_KIND_CHECK}},
		},
	} {
		if got := describeWait(w, "t"); got != want {
			t.Errorf("%q, want %q", got, want)
		}
	}
}

func TestAPoolSaysWhoHoldsItsGPUsAndWhoWaits(t *testing.T) {
	grant := func(whole bool, devices ...uint32) *cliv1.GpuGrant {
		return &cliv1.GpuGrant{Pool: "gpu:local", PoolSize: 8, Devices: devices, Whole: whole, Source: cliv1.GrantSource_GRANT_SOURCE_CLI}
	}
	held := describeGPUHolders([]*cliv1.GpuHolder{
		{JobId: "j41", Kind: cliv1.JobKind_JOB_KIND_CHECK, RunId: "r7", Gpus: grant(false, 0, 1)},
		{JobId: "j42", Kind: cliv1.JobKind_JOB_KIND_COMMAND, Gpus: grant(false, 4, 5, 6, 7)},
	})
	if want := "j41 (check, run r7) on devices 0-1, j42 (command) on devices 4-7"; held != want {
		t.Errorf("held: %q, want %q", held, want)
	}
	if got := describeGPUHolders([]*cliv1.GpuHolder{{JobId: "j9", Kind: cliv1.JobKind_JOB_KIND_MEASURE, Gpus: grant(true, 0, 1, 2, 3, 4, 5, 6, 7)}}); got != "j9 (measure) on devices 0-7 (whole node)" {
		t.Errorf("whole: %q", got)
	}
	if got := describeGPUWaiters([]*cliv1.GpuWaiter{{JobId: "j43", Wanted: 4}, {JobId: "j44", Wanted: 1}}); got != "j43 (4 GPUs), j44 (1 GPU)" {
		t.Errorf("waiting: %q", got)
	}
}
