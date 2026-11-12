package main

import (
	"encoding/json"
	"strings"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/alloc"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
	"github.com/uw-syfi/vibesys/cli/internal/store/memstore"
)

// onPlacementP lets a job run on the placement "p", claiming one of its slots.
type onPlacementP struct{}

func (onPlacementP) Options(core.Job) []schedule.Option {
	return []schedule.Option{{Placement: "p", Claims: []schedule.Claim{{Resource: schedule.PlacementOf("p"), Amount: 1}}}}
}

// allocCLI is a command line over a daemon whose placement "p" offers standing allocations as
// slots of its own capacity.
func allocCLI(t *testing.T) *cli {
	t.Helper()
	c := newCLI(t)
	c.world.Scheduler = app.Scheduler{Resources: []schedule.Resource{{ID: schedule.PlacementOf("p"), Capacity: 2}}, Demand: onPlacementP{}}
	c.world.Allocations = app.Allocations{Log: &memstore.AllocLog{}, Placements: []app.AllocPlacement{{Name: "p", Limits: alloc.Limits{
		MaxHeld: 2, MaxFor: 2 * time.Hour, Margin: time.Minute, Endless: true,
	}}}}
	c.env["VIBESYS_RUN"] = "run-7"
	return c
}

func (c *cli) allocOK(args ...string) result {
	c.t.Helper()
	r := c.run(append([]string{"alloc"}, args...)...)
	if r.code != exitOK {
		c.t.Fatalf("alloc %v: exit %d: %s", args, r.code, r.stderr)
	}
	return r
}

// An allocation is requested, waited for, listed, shown and ended from the command line, and a job
// submitted into it runs there.
func TestAnAllocationIsDrivenFromTheCommandLine(t *testing.T) {
	c := allocCLI(t)
	out := c.allocOK("request", "p", "--for", "1h").stdout
	if !strings.HasPrefix(out, "a1\t") || !strings.Contains(out, "\tp\trun-7\tfor 1h0m0s") {
		t.Fatalf("request printed %q", out)
	}
	if got := c.allocOK("wait", "a1", "--until", "running", "--timeout", "1h").stdout; !strings.HasPrefix(got, "a1\trunning\t") {
		t.Fatalf("wait --until running printed %q", got)
	}
	var shown struct {
		Allocation struct {
			ID    string `json:"id"`
			State string `json:"state"`
		} `json:"allocation"`
	}
	if err := json.Unmarshal([]byte(c.allocOK("show", "a1", "--json").stdout), &shown); err != nil || shown.Allocation.ID != "a1" || shown.Allocation.State != "ALLOCATION_STATE_RUNNING" {
		t.Fatalf("show --json = %+v (%v), want a1 running", shown, err)
	}
	if got := c.allocOK("ls").stdout; !strings.HasPrefix(got, "a1\trunning\t") {
		t.Fatalf("ls printed %q", got)
	}
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--alloc", "a1", "--json", "--", "print", "hi", ";", "exit", "0")...)
	if r.code != exitOK {
		t.Fatalf("submit --alloc a1: exit %d: %s", r.code, r.stderr)
	}
	if got := c.allocOK("end", "a1", "--after-jobs").stdout; !strings.HasPrefix(got, "a1\t") {
		t.Fatalf("end printed %q", got)
	}
	if r := c.run("alloc", "wait", "a1", "--until", "ended", "--timeout", "1h"); r.code != exitOK {
		t.Fatalf("wait --until ended: exit %d: %s", r.code, r.stderr)
	}
	if got := c.allocOK("ls").stdout; got != "" {
		t.Fatalf("ls lists %q after the allocation ended, want nothing", got)
	}
	if got := c.allocOK("ls", "--ended").stdout; !strings.Contains(got, "ended") {
		t.Fatalf("ls --ended printed %q", got)
	}
}

// Mistakes in the arguments are named before the daemon is asked, and a refusal of the daemon
// surfaces as the failure kind that says whose mistake it is.
func TestAllocationCommandsRefuseWhatCannotBeRight(t *testing.T) {
	c := allocCLI(t)
	delete(c.env, "VIBESYS_RUN")
	for _, tc := range []struct {
		name string
		args []string
		code int
		in   string
	}{
		{"request with no run", []string{"alloc", "request", "p", "--for", "1h"}, exitInvalidInput, "--run"},
		{"no subcommand", []string{"alloc"}, exitInvalidInput, "subcommand"},
		{"unknown subcommand", []string{"alloc", "park"}, exitInvalidInput, "park"},
		{"request with no time", []string{"alloc", "request", "p", "--run", "r"}, exitInvalidInput, "--for"},
		{"wait with no state", []string{"alloc", "wait", "a1"}, exitInvalidInput, "--until"},
		{"wait ending-in with no time", []string{"alloc", "wait", "a1", "--until", "ending-in"}, exitInvalidInput, "ending-in"},
		{"wait unbounded", []string{"alloc", "wait", "a1", "--until", "running", "--timeout", "0"}, exitInvalidInput, "bounded"},
		{"renew with no until", []string{"alloc", "renew", "a1", "--run", "r", "--lead", "1m", "--for", "1h"}, exitInvalidInput, "--until"},
		{"renew off with a lead", []string{"alloc", "renew", "a1", "--run", "r", "--off", "--lead", "1m"}, exitInvalidInput, "--off"},
		{"a placement with no allocations", []string{"alloc", "request", "q", "--run", "r", "--for", "1h"}, exitInvalidInput, "allocations"},
		{"an allocation nobody has", []string{"alloc", "show", "a9"}, exitInvalidInput, "a9"},
	} {
		r := c.run(tc.args...)
		if r.code != tc.code || !strings.Contains(r.stderr, tc.in) {
			t.Errorf("%s: exit %d, stderr %q; want exit %d naming %q", tc.name, r.code, r.stderr, tc.code, tc.in)
		}
	}
}

// A wait that times out exits 3, and a wait on an allocation that ended before the state exits 1.
func TestAnAllocationWaitsExitAsTheirOutcomeSays(t *testing.T) {
	for _, tc := range []struct {
		outcome cliv1.AllocationWaitOutcome
		want    int
	}{
		{cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_MET, exitOK},
		{cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_TIMED_OUT, exitTimeout},
		{cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_ENDED, exitFailed},
		{cliv1.AllocationWaitOutcome_ALLOCATION_WAIT_OUTCOME_UNSPECIFIED, exitNoAnswer},
		{cliv1.AllocationWaitOutcome(99), exitNoAnswer},
	} {
		if got := exitOfAllocWait(tc.outcome); got != tc.want {
			t.Errorf("exitOfAllocWait(%s) = %d, want %d", tc.outcome, got, tc.want)
		}
	}
}

// A local placement's capacity is the slots of its max_jobs, and an allocation holds one of them,
// listed as held for the run that has it.
func TestPlacementCapacityCountsTheSlotsAnAllocationHolds(t *testing.T) {
	c := allocCLI(t)
	type capacity struct {
		Capacity struct {
			Placement  string `json:"placement"`
			SlotsTotal uint32 `json:"slots_total"`
			SlotsFree  uint32 `json:"slots_free"`
			Held       []struct {
				ID string `json:"id"`
			} `json:"held"`
		} `json:"capacity"`
	}
	read := func() capacity {
		t.Helper()
		r := c.run("placement", "capacity", "p", "--json")
		if r.code != exitOK {
			t.Fatalf("placement capacity: exit %d: %s", r.code, r.stderr)
		}
		var got capacity
		if err := json.Unmarshal([]byte(r.stdout), &got); err != nil {
			t.Fatal(err)
		}
		return got
	}
	if got := read(); got.Capacity.SlotsTotal != 2 || got.Capacity.SlotsFree != 2 || len(got.Capacity.Held) != 0 {
		t.Fatalf("before any allocation: %+v, want 2 of 2 free and none held", got)
	}
	c.allocOK("request", "p", "--for", "1h")
	c.allocOK("wait", "a1", "--until", "running", "--timeout", "1h")
	if got := read(); got.Capacity.SlotsFree != 1 || len(got.Capacity.Held) != 1 || got.Capacity.Held[0].ID != "a1" {
		t.Fatalf("with a1 running: %+v, want 1 free and a1 held", got)
	}
	if r := c.run("placement", "capacity", "nowhere"); r.code == exitOK || !strings.Contains(r.stderr, "nowhere") {
		t.Fatalf("an unknown placement: exit %d, stderr %q; want a failure naming it", r.code, r.stderr)
	}
	if r := c.run("placement", "capacity"); r.code != exitInvalidInput {
		t.Fatalf("no placement: exit %d, want invalid input", r.code)
	}
}
