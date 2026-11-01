package main

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"sync"
	"testing"
	"time"

	"repoctl/execution"
)

func gateFixture() (graph, plan) {
	g, p := testFixture()
	g.CheckGroups = map[string]execution.Suite{
		"always": {Name: "always", IncludeInChecks: true, Language: "python", Directory: ".", TimeoutSeconds: 1,
			Commands: [][]string{{"slow-scan"}, {"cheap-scan"}}, GateCommands: [][]string{{"cheap-scan"}}},
		"python": {Name: "python", TriggerJob: "python", IncludeInTest: true, Language: "python", Directory: ".", TimeoutSeconds: 1,
			Commands: [][]string{{"pytest"}}},
		"types": {Name: "types", TriggerJob: "python", IncludeInChecks: true, Language: "python", Directory: ".", TimeoutSeconds: 1,
			Commands: [][]string{{"ty", "all"}}, GateCommands: [][]string{{"ty", "gate"}}},
		"clients_quality": {Name: "clients_quality", TriggerJob: "clients", IncludeInChecks: true, Language: "typescript", Directory: "clients",
			Collection: "pnpm_packages", Commands: [][]string{{"pnpm", "all"}}, PackageCommands: [][]string{{"pnpm", "{package}", "full"}},
			GateCommands: [][]string{{"pnpm", "check"}}},
		"clients_tests": {Name: "clients_tests", TriggerJob: "clients", IncludeInTest: true, Language: "typescript", Directory: "clients",
			Collection: "pnpm_packages", Commands: [][]string{{"pnpm", "test"}}, PackageCommands: [][]string{{"pnpm", "{package}", "full"}},
			GatePackageCommands: [][]string{{"pnpm", "{package}", "gate"}}},
		"docker": {Name: "docker", TriggerJob: "python", LocalSkip: "needs Docker", Language: "python", Directory: ".", TimeoutSeconds: 1,
			Commands: [][]string{{"docker", "info"}}},
	}
	p.ChangedPaths = []string{"src/a.py"}
	return g, p
}

func laneCommands(lanes []gateLane) map[string][][]string {
	got := map[string][][]string{}
	for _, lane := range lanes {
		for _, check := range lane.Checks {
			got[lane.Group] = append(got[lane.Group], check.Args)
		}
	}
	return got
}

func TestGateRunsEachSelectedGroupsGateCommandsAndNothingElse(t *testing.T) {
	g, p := gateFixture()
	lanes, err := selectedGateLanes(g, p)
	if err != nil {
		t.Fatal(err)
	}
	want := map[string][][]string{
		"always": {{"cheap-scan"}},
		"types":  {{"ty", "gate"}},
		// A gate_commands group runs them once even when packages are selected; the
		// package expansion belongs to gate_package_commands.
		"clients_quality": {{"pnpm", "check"}},
		"clients_tests":   {{"pnpm", "@example/a", "gate"}, {"pnpm", "@example/z", "gate"}},
	}
	if got := laneCommands(lanes); !reflect.DeepEqual(got, want) {
		t.Fatalf("lanes = %v\nwant %v", got, want)
	}
	for i := 1; i < len(lanes); i++ {
		if lanes[i-1].Group >= lanes[i].Group {
			t.Fatalf("lanes out of order: %s before %s", lanes[i-1].Group, lanes[i].Group)
		}
	}
}

func TestGateSkipsGroupsWhoseJobIsNotSelectedAndAlwaysRunsTheAlwaysOnOnes(t *testing.T) {
	g, p := gateFixture()
	p.Jobs = map[string]bool{}
	lanes, err := selectedGateLanes(g, p)
	if err != nil || !reflect.DeepEqual(laneCommands(lanes), map[string][][]string{"always": {{"cheap-scan"}}}) {
		t.Fatalf("lanes = %v, err = %v", laneCommands(lanes), err)
	}
	p.ChangedPaths = nil
	if lanes, err = selectedGateLanes(g, p); err != nil || len(lanes) != 0 {
		t.Fatalf("nothing changed, yet lanes = %v, err = %v", laneCommands(lanes), err)
	}
}

func TestGateHonorsLocalPaths(t *testing.T) {
	g, p := gateFixture()
	suite := g.CheckGroups["types"]
	suite.LocalPaths = []string{"libs"}
	g.CheckGroups["types"] = suite
	lanes, _ := selectedGateLanes(g, p)
	if _, ok := laneCommands(lanes)["types"]; ok {
		t.Fatal("types ran though no changed path is under its local_paths")
	}
	p.ChangedPaths = []string{"libs/x.py"}
	lanes, _ = selectedGateLanes(g, p)
	if _, ok := laneCommands(lanes)["types"]; !ok {
		t.Fatal("types did not run for a changed path under its local_paths")
	}
}

func TestGateCommandsMustBeValid(t *testing.T) {
	g, _ := gateFixture()
	g.Jobs = []string{"python", "clients"}
	g.Collections = []collectionSpec{{Name: "pnpm_packages"}}
	bad := []execution.Suite{
		{Name: "skip", TriggerJob: "python", LocalSkip: "needs Docker", Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"x"}}, GateCommands: [][]string{{"x"}}},
		{Name: "empty", TriggerJob: "python", IncludeInChecks: true, Language: "python", Directory: ".", TimeoutSeconds: 1, Commands: [][]string{{"x"}}, GateCommands: [][]string{{}}},
		{Name: "nopackage", TriggerJob: "clients", IncludeInChecks: true, Language: "typescript", Directory: "clients", Collection: "pnpm_packages", TimeoutSeconds: 1, Commands: [][]string{{"x"}}, PackageCommands: [][]string{{"x", "{package}"}}, GatePackageCommands: [][]string{{"x", "no placeholder"}}},
		{Name: "nocollection", TriggerJob: "clients", IncludeInChecks: true, Language: "typescript", Directory: "clients", TimeoutSeconds: 1, Commands: [][]string{{"x"}}, GatePackageCommands: [][]string{{"x", "{package}"}}},
	}
	for _, suite := range bad {
		g.CheckGroups = map[string]execution.Suite{}
		if err := g.validateCheckGroups([]execution.Suite{suite}); err == nil {
			t.Fatalf("accepted invalid gate configuration %+v", suite)
		}
	}
}

type gateRunner struct {
	out   io.Writer
	fail  string
	block string
	log   *[]string
	mu    *sync.Mutex
}

func (r gateRunner) Run(ctx context.Context, _ string, check execution.Check) error {
	name := strings.Join(check.Args, " ")
	r.mu.Lock()
	*r.log = append(*r.log, name)
	r.mu.Unlock()
	fmt.Fprintf(r.out, "output of %s\n", name)
	if name == r.block {
		<-ctx.Done()
		return ctx.Err()
	}
	if name == r.fail {
		return fmt.Errorf("%s: exit status 1", name)
	}
	return nil
}

func lane(group string, commands ...string) gateLane {
	checks := []execution.Check{}
	for _, command := range commands {
		checks = append(checks, execution.Check{Label: group, Directory: ".", Args: strings.Fields(command)})
	}
	return gateLane{Group: group, Checks: checks}
}

func TestLanesRunConcurrentlyAndAFailureCancelsTheRest(t *testing.T) {
	var log []string
	var mu sync.Mutex
	newRunner := func(w io.Writer) execution.Runner {
		return gateRunner{out: w, fail: "fmt bad", block: "types slow", log: &log, mu: &mu}
	}
	// "types" blocks until another lane fails, so the run can only finish if the lanes
	// really run at the same time and a failure cancels the others.
	lanes := []gateLane{lane("format", "fmt ok", "fmt bad", "fmt never"), lane("types", "types slow")}
	results := runLanes(context.Background(), ".", lanes, newRunner)
	if results[0].Err == nil || results[1].Err == nil {
		t.Fatalf("results = %+v", results)
	}
	for _, name := range log {
		if name == "fmt never" {
			t.Fatal("a lane kept running its checks after one failed")
		}
	}
	var out bytes.Buffer
	err := reportGate(&out, results, 3*time.Second, context.Background())
	text := out.String()
	for _, want := range []string{"fmt bad: exit status 1", "output of fmt ok", "format", "FAILED", "cancelled", "push refused", gateSkipEnv} {
		if !strings.Contains(text, want) {
			t.Errorf("report lacks %q:\n%s", want, text)
		}
	}
	if err == nil || !strings.Contains(err.Error(), "format") || strings.Contains(err.Error(), "types") {
		t.Fatalf("error = %v, want it to name only the lane that failed", err)
	}
}

func TestAPassingGateSaysSoWithItsTime(t *testing.T) {
	var out bytes.Buffer
	results := []laneResult{{Group: "format", Duration: time.Second}}
	if err := reportGate(&out, results, 42*time.Second, context.Background()); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(out.String(), "passed in 42.0s") {
		t.Fatalf("report = %q", out.String())
	}
}

func TestAHungGateIsStoppedByItsDeadlineAndRefusesThePush(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Millisecond)
	defer cancel()
	var log []string
	var mu sync.Mutex
	newRunner := func(w io.Writer) execution.Runner {
		return gateRunner{out: w, block: "types hang", log: &log, mu: &mu}
	}
	results := runLanes(ctx, ".", []gateLane{lane("types", "types hang")}, newRunner)
	var out bytes.Buffer
	if err := reportGate(&out, results, time.Second, ctx); err == nil {
		t.Fatalf("a gate that hit its deadline passed:\n%s", out.String())
	}
	if !strings.Contains(out.String(), "stopped") {
		t.Fatalf("report does not say the gate was stopped:\n%s", out.String())
	}
}

func TestTheEmergencySkipIsLoggedWithItsReason(t *testing.T) {
	root := t.TempDir()
	for _, args := range [][]string{{"init", "-q"}, {"config", "user.email", "t@example.invalid"}, {"config", "user.name", "t"}, {"commit", "-q", "--allow-empty", "-m", "x"}} {
		cmd := exec.Command("git", args...)
		cmd.Dir = root
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	var out bytes.Buffer
	now := time.Date(2026, 10, 31, 12, 0, 0, 0, time.UTC)
	if err := skipGate(root, "prod  is\tdown", now, &out); err != nil {
		t.Fatal(err)
	}
	logged, err := os.ReadFile(filepath.Join(root, ".git", gateSkipLog))
	if err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(string(logged), "2026-10-31T12:00:00Z") || !strings.HasSuffix(strings.TrimSpace(string(logged)), "prod is down") {
		t.Fatalf("log = %q", logged)
	}
	if !strings.Contains(out.String(), "SKIPPED") {
		t.Fatalf("the skip was not announced: %q", out.String())
	}
}

func TestTheTrackedHookIsExecutableAndRunsTheGate(t *testing.T) {
	hook := filepath.Join("..", "..", gateHooksDir, "pre-push")
	info, err := os.Stat(hook)
	if err != nil {
		t.Fatal(err)
	}
	if info.Mode()&0o111 == 0 {
		t.Fatal("the pre-push hook is not executable, so git would ignore it")
	}
	body, err := os.ReadFile(hook)
	if err != nil || !strings.Contains(string(body), "repoctl gate") {
		t.Fatalf("hook does not call `repoctl gate`: %v", err)
	}
}

func repositoryGateLanes(t *testing.T, paths []string) map[string][][]string {
	t.Helper()
	g, err := readPolicy("../..", ".repoctl")
	if err != nil {
		t.Fatal(err)
	}
	g = g.forLocalTest()
	p, err := g.selectPaths(paths)
	if err != nil {
		t.Fatal(err)
	}
	lanes, err := selectedGateLanes(g, p)
	if err != nil {
		t.Fatal(err)
	}
	return laneCommands(lanes)
}

func TestTheRepositoryGateFollowsTheChange(t *testing.T) {
	cases := []struct {
		name  string
		paths []string
		want  []string
	}{
		{"python only", []string{"scripts/check_purity.py"}, []string{"always_on", "python_impact_tests", "python_quality", "python_types"}},
		{"clients only", []string{"clients/libs/run-state/src/reduce.ts"}, []string{"always_on", "clients_quality", "clients_tests"}},
		{"docs only", []string{"docs/contributing/development.md"}, []string{"always_on"}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			got := []string{}
			for group := range repositoryGateLanes(t, tc.paths) {
				got = append(got, group)
			}
			sortStrings(got)
			for _, want := range tc.want {
				if !contains(got, want) {
					t.Errorf("gate lanes %v lack %q", got, want)
				}
			}
			for _, group := range got {
				if !contains(tc.want, group) {
					t.Errorf("gate lane %q is not wanted for %s", group, tc.name)
				}
			}
		})
	}
}
