package production

import (
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// test-isolation: machineEnvironments is the wiring from the operator config to each placement's
// environment, which RunDaemon reaches only with a real runtime directory and index.

// Every placement of every kind is wired without reaching Docker or another machine, and offers the
// resources its section names, the AMD GPUs of an apptainer placement among them, with the claims
// its resources make.
func TestEveryPlacementKindIsWired(t *testing.T) {
	p := withApptainer(t)
	for _, text := range configs {
		op := parse(t, text)
		m, err := p.machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
		if err != nil {
			t.Fatalf("config %q: %v", text, err)
		}
		if len(m.offers) != len(op.GetPlacements()) {
			t.Fatalf("config %q: offers %+v", text, m.offers)
		}
		for _, offer := range m.offers {
			pl := op.GetPlacements()[offer.Name]
			if rocm := pl.GetResources().GetRocm() != nil; rocm != slices.Contains(offer.Resources, core.ResourceROCm) {
				t.Errorf("config %q: placement %s offers %v", text, offer.Name, offer.Resources)
			}
		}
		if got, want := m.claims, claimsOf(t, text); len(want) > 0 && !sameClaims(got, want) {
			t.Errorf("config %q: claims %v, want %v", text, got, want)
		}
	}
}

// withApptainer is a process whose PATH has an apptainer, with which the apptainer placements
// convert their images on this machine.
func withApptainer(t *testing.T) Process {
	t.Helper()
	bin := t.TempDir()
	if err := os.WriteFile(filepath.Join(bin, "apptainer"), []byte("#!/bin/sh\n"), 0o755); err != nil {
		t.Fatal(err)
	}
	getenv := func(k string) string {
		if k == "PATH" {
			return bin
		}
		return ""
	}
	return Process{UID: 1000, GID: 1000, Home: "/home/u", Executable: "/usr/bin/vibesys-cli", Getenv: getenv}
}

// twoSlices is an operator config of two apptainer placements on one cluster, each of whose
// allocations is a slice of a node with its own GPUs.
const twoSlices = `
[placements.mi210-a]
kind = "apptainer"
[placements.mi210-a.remote]
host = "host-a"
remote_workspace_root = "/w"
[placements.mi210-a.slurm]
partition = "gpu"
exclusive = false
[placements.mi210-a.resources.rocm]
[placements.mi210-b]
kind = "apptainer"
[placements.mi210-b.remote]
host = "host-a"
remote_workspace_root = "/w"
[placements.mi210-b.slurm]
partition = "gpu"
exclusive = false
[placements.mi210-b.resources.rocm]
`

// A job of an apptainer placement gets the GPUs of its own allocation, so the scheduler of the
// operator config holds them per placement: a measure on one placement of a cluster does not wait
// for a job on another placement of it, nor a job there for the measure, and a measure still
// excludes every other job of its own placement, and waits for one that runs there.
func TestAMeasureHoldsTheGPUsOfItsOwnApptainerPlacementOnly(t *testing.T) {
	op := parse(t, twoSlices)
	m, err := withApptainer(t).machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
	if err != nil {
		t.Fatal(err)
	}
	s, err := schedulerOf(op, m.offers, m.claims)
	if err != nil {
		t.Fatal(err)
	}
	claims := func(placement string, kind core.JobKind) []schedule.Claim {
		spec := core.JobSpec{Environment: core.EnvironmentDocker, Placement: placement}
		if kind == core.JobMeasure {
			spec.Settings = core.MeasureSettings{}
		}
		options := s.Demand.Options(core.Job{Spec: spec})
		if len(options) != 1 {
			t.Fatalf("a %s job restricted to %s may run on %+v", kind, placement, options)
		}
		return options[0].Claims
	}
	cases := []struct {
		running, waiting       string // the placement of each job
		runningKind, waitsKind core.JobKind
		starts                 bool
	}{
		{"mi210-b", "mi210-a", core.JobCommand, core.JobMeasure, true},
		{"mi210-b", "mi210-a", core.JobMeasure, core.JobCommand, true},
		{"mi210-b", "mi210-a", core.JobMeasure, core.JobMeasure, true},
		{"mi210-a", "mi210-a", core.JobCommand, core.JobMeasure, false},
		{"mi210-a", "mi210-a", core.JobMeasure, core.JobCommand, false},
	}
	for _, c := range cases {
		state := schedule.State{
			Resources: s.Resources,
			Holdings:  []schedule.Holding{{ID: "running", Claims: claims(c.running, c.runningKind)}},
			Waiting:   []schedule.Candidate{{ID: "waiting", Options: []schedule.Option{{Placement: c.waiting, Claims: claims(c.waiting, c.waitsKind)}}}},
		}
		decisions := schedule.Step(state, schedule.Rules{Policy: s.Policy})
		_, started := decisions[0].(schedule.Start)
		if started != c.starts {
			t.Errorf("a %s job on %s waits beside a %s job on %s: %+v, want started %v",
				c.waitsKind, c.waiting, c.runningKind, c.running, decisions[0], c.starts)
		}
	}
}

// An apptainer placement on a machine without apptainer on the daemon's PATH is refused, saying to
// install it, and the placement's local scratch directory defaults under the user's cache.
func TestAnApptainerPlacementNeedsApptainerHere(t *testing.T) {
	p := Process{UID: 1000, GID: 1000, Home: "/home/u", Executable: "/usr/bin/vibesys-cli", Getenv: func(string) string { return "" }}
	op := parse(t, configs[len(configs)-2])
	_, err := p.machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
	if err == nil || !strings.Contains(err.Error(), "install Apptainer") {
		t.Errorf("without apptainer: %v", err)
	}
	for env, want := range map[string]string{"": "/home/u/.cache/vibesys-cli/apptainer", "/xdg": "/xdg/vibesys-cli/apptainer", "rel": "/home/u/.cache/vibesys-cli/apptainer"} {
		p.Getenv = func(k string) string {
			if k == "XDG_CACHE_HOME" {
				return env
			}
			return ""
		}
		if got := p.localScratch(nil); got != want {
			t.Errorf("XDG_CACHE_HOME=%q: %s, want %s", env, got, want)
		}
	}
}

// An apptainer placement of the pyxis runtime converts nothing on this machine, so it needs no
// apptainer here: the login node makes the images' squashfs.
func TestAPyxisPlacementNeedsNoApptainerHere(t *testing.T) {
	p := Process{UID: 1000, GID: 1000, Home: "/home/u", Executable: "/usr/bin/vibesys-cli", Getenv: func(string) string { return "" }}
	op := parse(t, `
[placements.mi300]
kind = "apptainer"
[placements.mi300.remote]
host = "host-a"
remote_workspace_root = "/scratch/user/vibesys"
[placements.mi300.slurm]
partition = "part-a"
gres = "gpu:4"
[placements.mi300.apptainer]
runtime = "pyxis"
submit_arguments = ["--account=acct-a"]
[placements.mi300.resources.rocm]
`)
	if _, err := p.machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key"); err != nil {
		t.Errorf("a pyxis placement without apptainer here: %v", err)
	}
}

func sameClaims(a, b map[string][]core.Claim) bool {
	for name, cs := range b {
		if !slices.Equal(a[name], cs) {
			return false
		}
	}
	return true
}

// threeAtOnce is an operator config of one apptainer placement that runs three jobs at once, each
// in allocations of its own, a measured one of which holds a whole node with [slurm] exclusive.
func threeAtOnce(exclusive string) string {
	return `
[placements.gpus]
kind = "apptainer"
max_jobs = 3
[placements.gpus.remote]
host = "host-a"
remote_workspace_root = "/w"
[placements.gpus.slurm]
partition = "gpu"
` + exclusive + `
[placements.gpus.resources.rocm]
`
}

// An apptainer placement of max_jobs jobs runs that many at once, since each has allocations of its
// own: the scheduler starts a job beside up to max_jobs - 1 others. With [slurm] exclusive (the
// default), a measured container's allocation is a whole node no other job shares, so a measure is
// already alone where it runs and takes a slot as any job does: measures of any task, and other
// jobs, run beside it, up to max_jobs. With exclusive = false, its allocation is a slice of a node,
// so a measure still runs alone on its placement: it waits for every other job there, and every
// other job waits for it.
func TestAnApptainerPlacementRunsMaxJobsAtOnceAndAMeasureAloneOnItsNode(t *testing.T) {
	cmd, measure := core.JobCommand, core.JobMeasure
	type step struct {
		running []core.JobKind
		waits   core.JobKind
		starts  bool
	}
	slots := []step{
		{[]core.JobKind{cmd}, cmd, true},
		{[]core.JobKind{cmd, cmd}, cmd, true},
		{[]core.JobKind{cmd, cmd, cmd}, cmd, false},
		{nil, measure, true},
	}
	for _, c := range []struct {
		exclusive string
		cases     []step
	}{
		{"", append(slots,
			step{[]core.JobKind{cmd}, measure, true},
			step{[]core.JobKind{measure}, cmd, true},
			step{[]core.JobKind{measure}, measure, true},
			step{[]core.JobKind{measure, measure}, measure, true},
			step{[]core.JobKind{measure, cmd, measure}, measure, false},
		)},
		{"exclusive = true", append(slots, step{[]core.JobKind{measure, cmd}, measure, true})},
		{"exclusive = false", append(slots,
			step{[]core.JobKind{cmd}, measure, false},
			step{[]core.JobKind{measure}, cmd, false},
			step{[]core.JobKind{measure}, measure, false},
		)},
	} {
		op := parse(t, threeAtOnce(c.exclusive))
		m, err := withApptainer(t).machineEnvironments(op, clock.System{}, runtimedir.Dir{}, "key")
		if err != nil {
			t.Fatal(err)
		}
		s, err := schedulerOf(op, m.offers, m.claims)
		if err != nil {
			t.Fatal(err)
		}
		claims := func(kind core.JobKind) []schedule.Claim {
			spec := core.JobSpec{Environment: core.EnvironmentDocker, Placement: "gpus"}
			if kind == core.JobMeasure {
				spec.Settings = core.MeasureSettings{}
			}
			options := s.Demand.Options(core.Job{Spec: spec})
			if len(options) != 1 {
				t.Fatalf("a %s job may run on %+v", kind, options)
			}
			return options[0].Claims
		}
		for _, k := range c.cases {
			state := schedule.State{
				Resources: s.Resources,
				Waiting:   []schedule.Candidate{{ID: "waiting", Options: []schedule.Option{{Placement: "gpus", Claims: claims(k.waits)}}}},
			}
			for i, kind := range k.running {
				state.Holdings = append(state.Holdings, schedule.Holding{ID: schedule.ID("running-" + strconv.Itoa(i)), Claims: claims(kind)})
			}
			decisions := schedule.Step(state, schedule.Rules{Policy: s.Policy})
			_, started := decisions[0].(schedule.Start)
			if started != k.starts {
				t.Errorf("[slurm] %q: a %s job waits beside %v: %+v, want started %v", c.exclusive, k.waits, k.running, decisions[0], k.starts)
			}
		}
	}
}
