package main

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"slices"
	"strconv"
	"strings"
	"testing"
)

// planNumbers is the first field of each line a plan listing prints.
func planNumbers(listing string) []string {
	var out []string
	for _, line := range strings.Split(strings.TrimRight(listing, "\n"), "\n") {
		if line != "" {
			out = append(out, strings.Split(line, "\t")[0])
		}
	}
	return out
}

// A plan is ready while it is open and every plan it depends on is won: list --ready names the
// ones a lead can start, and a plan that builds on others becomes ready once they are won.
func TestPlanListReadyFollowsDependencies(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	if r := c.planOK("create", "--title", "fuse the attention kernel", "--kind", "kernel"); r.stdout != "1\topen\tfuse the attention kernel\tkind kernel\n" {
		t.Fatalf("create = %q", r.stdout)
	}
	c.planOK("create", "--title", "page the kv cache", "--kind", "layout")
	r := c.planOK("create", "--title", "merge both", "--kind", "integration", "--depends-on", "#2,1")
	if r.stdout != "3\topen\tmerge both\tkind integration\tdepends on 1,2\n" {
		t.Fatalf("create with dependencies = %q", r.stdout)
	}
	c.planOK("create", "--title", "probe the scheduler", "--kind", "probe", "--depends-on", "1", "--depends-on", "3")

	ready := func() []string { return planNumbers(c.planOK("list", "--ready").stdout) }
	if got := ready(); !slices.Equal(got, []string{"1", "2"}) {
		t.Fatalf("ready before anything is won = %v, want 1 and 2", got)
	}
	c.planOK("close", "1", "--status", "won")
	if got := ready(); !slices.Equal(got, []string{"2"}) {
		t.Fatalf("ready with plan 1 won = %v, want 2: a won plan is not ready, and 3 waits for 2", got)
	}
	c.planOK("close", "2", "--status", "chosen")
	if got := ready(); len(got) != 0 {
		t.Fatalf("ready with plan 2 chosen = %v, want none: only a won dependency counts", got)
	}
	c.planOK("close", "2", "--status", "won")
	if got := ready(); !slices.Equal(got, []string{"3"}) {
		t.Fatalf("ready with 1 and 2 won = %v, want 3", got)
	}
	if got := planNumbers(c.planOK("list", "--ready", "--kind", "probe").stdout); len(got) != 0 {
		t.Fatalf("ready probes = %v, want none: plan 4 waits for 3", got)
	}
	if got := planNumbers(c.planOK("list", "--kind", "kernel,probe").stdout); !slices.Equal(got, []string{"1", "4"}) {
		t.Fatalf("list --kind kernel,probe = %v", got)
	}

	view := c.planOK("view", "3").stdout
	for _, want := range []string{"depends on:\n  1\twon\tfuse the attention kernel\tkernel\n  2\twon\tpage the kv cache\tlayout\n", "dependents:\n  4\topen\tprobe the scheduler\tprobe\n"} {
		if !strings.Contains(view, want) {
			t.Errorf("view 3 lacks %q:\n%s", want, view)
		}
	}
	var out struct {
		View struct {
			Plan struct {
				Kind      string   `json:"kind"`
				DependsOn []uint32 `json:"depends_on"`
			} `json:"plan"`
			DependsOn []struct {
				Number uint32 `json:"number"`
				Status string `json:"status"`
			} `json:"depends_on"`
			Dependents []struct {
				Number uint32 `json:"number"`
				Kind   string `json:"kind"`
			} `json:"dependents"`
		} `json:"view"`
	}
	if err := json.Unmarshal([]byte(c.planOK("view", "3", "--json").stdout), &out); err != nil {
		t.Fatal(err)
	}
	v := out.View
	if v.Plan.Kind != "PLAN_KIND_INTEGRATION" || !slices.Equal(v.Plan.DependsOn, []uint32{1, 2}) || len(v.DependsOn) != 2 ||
		v.DependsOn[0].Status != "PLAN_STATUS_WON" || len(v.Dependents) != 1 || v.Dependents[0].Number != 4 || v.Dependents[0].Kind != "PLAN_KIND_PROBE" {
		t.Fatalf("view 3 --json = %+v", v)
	}

	// --depends-on on edit replaces the set, none clears it, and --kind replaces the kind.
	if r := c.planOK("edit", "4", "--depends-on", "none", "--kind", "simulator"); r.stdout != "4\topen\tprobe the scheduler\tkind simulator\n" {
		t.Fatalf("edit to no dependencies = %q", r.stdout)
	}
	if got := ready(); !slices.Equal(got, []string{"3", "4"}) {
		t.Fatalf("ready after plan 4 lost its dependencies = %v", got)
	}
}

// The daemon refuses a dependency on a plan the task does not have, on the plan itself, and one that
// would close a cycle, and changes nothing.
func TestPlanDependencyRefusals(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	c.planOK("create", "--title", "a")
	c.planOK("create", "--title", "b", "--depends-on", "1")
	c.planOK("create", "--title", "c", "--depends-on", "2")
	for _, tc := range []struct {
		name string
		args []string
		want string
	}{
		{"an unknown plan on create", []string{"create", "--title", "d", "--depends-on", "9"}, "no plan 9"},
		{"an unknown plan on edit", []string{"edit", "1", "--depends-on", "9"}, "no plan 9"},
		{"itself", []string{"edit", "2", "--depends-on", "2"}, "itself"},
		{"a cycle", []string{"edit", "1", "--depends-on", "3"}, "1 -> 3 -> 2 -> 1"},
	} {
		r := c.plan(tc.args[0], tc.args[1:]...)
		if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q; want exit 2 and %q", tc.name, r.code, r.stdout, r.stderr, tc.want)
		}
	}
	if got := c.planOK("list").stdout; got != "1\topen\ta\n2\topen\tb\tdepends on 1\n3\topen\tc\tdepends on 2\n" {
		t.Fatalf("list after the refusals = %q", got)
	}
}

// A kind or a dependency the CLI cannot read is refused before the daemon is started.
func TestPlanKindAndDependencyMistakesNeverStartTheDaemon(t *testing.T) {
	c := newCLI(t)
	for _, tc := range []struct {
		name string
		args []string
		want string
	}{
		{"an unknown kind on create", []string{"plan", "create", "--task", "kv", "--title", "t", "--kind", "compiler"}, "compiler"},
		{"an unknown kind on edit", []string{"plan", "edit", "--task", "kv", "1", "--kind", "compiler"}, "compiler"},
		{"an unknown kind on list", []string{"plan", "list", "--task", "kv", "--kind", "kernel,compiler"}, "compiler"},
		{"a dependency that is not a number", []string{"plan", "create", "--task", "kv", "--title", "t", "--depends-on", "one"}, "one"},
		{"a dependency of zero", []string{"plan", "edit", "--task", "kv", "1", "--depends-on", "0"}, "plan number"},
		{"none and a number", []string{"plan", "edit", "--task", "kv", "1", "--depends-on", "none", "--depends-on", "2"}, "not both"},
	} {
		r := c.run(tc.args...)
		if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q; want exit 2, %q in stderr", tc.name, r.code, r.stdout, r.stderr, tc.want)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid invocation started a daemon")
	}
}

// Over random dependency graphs and statuses, list --ready --json names exactly the open plans whose
// dependencies are all won.
func TestPlanListReadyMatchesTheRuleOverRandomPlans(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	rng := rand.New(rand.NewPCG(43, 25))
	statuses := []string{"open", "chosen", "won", "parked", "rejected"}
	type plan struct {
		deps   []int
		status string
	}
	var plans []plan
	for n := 1; n <= 8; n++ {
		var deps []int
		var args []string
		for d := 1; d < n; d++ {
			if rng.IntN(3) == 0 {
				deps = append(deps, d)
				args = append(args, "--depends-on", strconv.Itoa(d))
			}
		}
		c.planOK("create", append([]string{"--title", fmt.Sprintf("p%d", n)}, args...)...)
		plans = append(plans, plan{deps: deps, status: "open"})
	}
	for round := range 4 {
		for i := range plans {
			if rng.IntN(2) == 0 {
				plans[i].status = statuses[rng.IntN(len(statuses))]
				c.planOK("edit", strconv.Itoa(i+1), "--status", plans[i].status)
			}
		}
		var want []uint32
		for i, p := range plans {
			if p.status == "open" && !slices.ContainsFunc(p.deps, func(d int) bool { return plans[d-1].status != "won" }) {
				want = append(want, uint32(i+1))
			}
		}
		var out struct {
			Plans []struct {
				Number uint32 `json:"number"`
			} `json:"plans"`
		}
		if err := json.Unmarshal([]byte(c.planOK("list", "--ready", "--json").stdout), &out); err != nil {
			t.Fatal(err)
		}
		var got []uint32
		for _, p := range out.Plans {
			got = append(got, p.Number)
		}
		if !slices.Equal(got, want) {
			t.Fatalf("round %d: list --ready = %v, want %v (plans %+v)", round, got, want, plans)
		}
	}
}
