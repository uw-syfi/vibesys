package main

import (
	"context"
	"encoding/json"
	"fmt"
	"math"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// The history the result commands run over: base, then a line of commits on it. The result
// branch of the task "kv" starts at base. Revisions are named "base", "c1", "c2" and so on.
func rev(n int) core.Commit { return core.Commit(strings.Repeat(fmt.Sprintf("%x", n+1), 40)) }

const resultBranch = "vibesys/result/kv"

var (
	win   = []float64{120, 121, 119, 122, 120}
	level = []float64{100, 101, 99, 100, 102}
	lose  = []float64{80, 81, 79, 80, 82}
)

// resultsCLI is the command line over a daemon with the repository's history, the branch at base,
// and a clock the tests read for the time results are recorded at.
type resultsCLI struct {
	*cli
	clk   *clock.Manual
	store store.Store
	// taskVersion is the version of the task every commit of the history has.
	taskVersion string
}

func newResultsCLI(t *testing.T) *resultsCLI {
	t.Helper()
	c := newCLI(t)
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	c.starter.Clock = clk
	revisions := map[string]core.Commit{"HEAD": full, "main": full, "base": rev(0)}
	c.world.Branches.AddCommit("/repo", rev(0))
	for n := 1; n <= 4; n++ {
		revisions[fmt.Sprintf("c%d", n)] = rev(n)
		c.world.Branches.AddCommit("/repo", rev(n), rev(n-1))
	}
	c.world.Branches.AddBranch("/repo", resultBranch, rev(0))
	for n := 0; n <= 4; n++ {
		c.world.Tasks.Put("/repo", rev(n), tasktest.Files("kv"))
	}
	tk, err := c.world.Tasks.Task(context.Background(), "/repo", rev(0), "kv")
	if err != nil {
		t.Fatal(err)
	}
	c.git.AddRepo("/repo", revisions)
	st, err := c.world.Stores.Create(context.Background(), scopeAt("/repo", "kv"))
	if err != nil {
		t.Fatal(err)
	}
	return &resultsCLI{cli: c, clk: clk, store: st, taskVersion: tk.Version}
}

func (c *resultsCLI) measured(n int, samples []float64) core.Result {
	c.t.Helper()
	return storetest.SeedMeasurement(c.t, c.store, storetest.Measurement{Commit: rev(n), Samples: samples, At: c.clk.Now()})
}

func (c *resultsCLI) branchAt() core.Commit {
	got, _ := c.world.Branches.At("/repo", resultBranch)
	return got
}

func mustExit(t *testing.T, r result, code int, args ...string) {
	t.Helper()
	if r.code != code {
		t.Fatalf("%v: exit %d, want %d; stdout %q, stderr %q", args, r.code, code, r.stdout, r.stderr)
	}
}

func TestPromoteMovesTheBranchAndPrintsWhatHappened(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	c.measured(1, win)

	r := c.run(withTask("promote", "c1")...)
	mustExit(t, r, exitOK, "promote c1")
	if !strings.Contains(r.stdout, "promoted "+string(rev(1))[:12]) || !strings.Contains(r.stdout, "better") || r.stderr != "" {
		t.Fatalf("promote = %+v", r)
	}
	if c.branchAt() != rev(1) {
		t.Fatalf("the branch is at %.7s", c.branchAt())
	}

	r = c.run(withTask("promote", "c1", "--json")...)
	// The same commit again is not better than itself: a refusal, and --json says why.
	mustExit(t, r, exitFailed, "promote c1 again")
	var resp cliv1.PromoteResponse
	if err := protojson.Unmarshal([]byte(r.stdout), &resp); err != nil || resp.GetRefused() == nil || len(resp.GetRefused().GetReasons()) == 0 {
		t.Fatalf("promote --json = %+v (%v)", r, err)
	}
}

// A refusal is an answer, not an error: stdout carries every reason with its evidence, stderr is
// empty, and the exit code says the branch did not move.
func TestAPromoteRefusalListsEveryReasonOnStdout(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	storetest.SeedMeasurement(t, c.store, storetest.Measurement{Commit: rev(2), Samples: lose, At: c.clk.Now(), CheckJob: "j-gone"})
	c.measured(1, win)
	mustExit(t, c.run(withTask("promote", "c1")...), exitOK)

	r := c.run(withTask("promote", "c2")...)
	mustExit(t, r, exitFailed, "promote c2")
	for _, want := range []string{"refused", "check_missing:", "worse:", "stale:", "measured against " + string(rev(0))[:12], "the branch is at " + string(rev(1))[:12]} {
		if !strings.Contains(r.stdout, want) {
			t.Errorf("stdout %q lacks %q", r.stdout, want)
		}
	}
	if r.stderr != "" || c.branchAt() != rev(1) {
		t.Fatalf("stderr %q, branch at %.7s", r.stderr, c.branchAt())
	}

	r = c.run(withTask("promote", "c2", "--json")...)
	var resp cliv1.PromoteResponse
	if err := protojson.Unmarshal([]byte(r.stdout), &resp); err != nil {
		t.Fatal(err)
	}
	var kinds []cliv1.PromoteRefusalKind
	for _, reason := range resp.GetRefused().GetReasons() {
		kinds = append(kinds, reason.GetKind())
	}
	want := []cliv1.PromoteRefusalKind{
		cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_CHECK_MISSING, cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_WORSE,
		cliv1.PromoteRefusalKind_PROMOTE_REFUSAL_KIND_STALE,
	}
	if fmt.Sprint(kinds) != fmt.Sprint(want) {
		t.Fatalf("reasons = %v, want %v", kinds, want)
	}
}

// Anything but a refusal that cannot be answered is an error: typed, on stderr, with an empty
// stdout, in JSON when asked.
func TestPromoteFailuresAreTypedErrors(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	c.measured(1, win)
	c.measured(3, []float64{150, 151, 149, 152, 150})
	mustExit(t, c.run(withTask("promote", "c1")...), exitOK)
	// c3 is better but its line contains c1, so this one fast-forwards; a commit off the line does not.
	c.world.Branches.AddCommit("/repo", rev(4), rev(0))
	c.git.AddRepo("/repo", map[string]core.Commit{"HEAD": full, "off": rev(4)})
	c.measured(4, []float64{160, 161, 159, 162, 160})

	r := c.run(withTask("promote", "off", "--json")...)
	var f struct{ Kind, Message string }
	if r.code != exitFailed || r.stdout != "" || json.Unmarshal([]byte(r.stderr), &f) != nil || f.Kind != "FAILURE_KIND_CONFLICT" || !strings.Contains(f.Message, "contain") {
		t.Fatalf("promote of a commit off the line: %+v", r)
	}
	if c.branchAt() != rev(1) {
		t.Fatalf("the branch moved to %.7s", c.branchAt())
	}

	r = c.run(withTask("promote", "no-such-rev")...)
	if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, "no-such-rev") {
		t.Fatalf("promote of an unknown revision: %+v", r)
	}
}

// Text rounds and JSON does not: for any samples, the JSON of a comparison holds the exact
// numbers the verdict has, and the text holds the numbers rounded to four digits.
func TestCompareRoundsTextAndKeepsJSONExact(t *testing.T) {
	rng := rand.New(rand.NewPCG(3, 5))
	samples := func() []float64 {
		out := make([]float64, 3+rng.IntN(5))
		level := 10 + rng.Float64()*1000
		for i := range out {
			out[i] = level * (1 + (rng.Float64()-0.5)*0.05)
		}
		return out
	}
	for i := range 25 {
		c := newResultsCLI(t)
		candidate, base := samples(), samples()
		c.measured(0, base)
		c.measured(1, candidate)

		r := c.run(withTask("compare", "c1", "--json")...)
		mustExit(t, r, exitOK, "compare --json")
		var resp cliv1.CompareResponse
		if err := protojson.Unmarshal([]byte(r.stdout), &resp); err != nil {
			t.Fatalf("example %d: %v", i, err)
		}
		want, err := core.Compare(core.CompareInput{Direction: core.Maximize, Candidate: candidate, Base: base})
		if err != nil {
			t.Fatal(err)
		}
		v := resp.GetComparison().GetVerdict()
		if math.Float64bits(v.GetRatio()) != math.Float64bits(want.Ratio) || math.Float64bits(v.GetNoise()) != math.Float64bits(want.Noise) ||
			math.Float64bits(v.GetCandidateMean()) != math.Float64bits(want.CandidateMean) {
			t.Fatalf("example %d: JSON verdict %+v, want %+v exactly", i, v, want)
		}

		text := c.run(withTask("compare", "c1")...)
		mustExit(t, text, exitOK, "compare")
		rounded := strconv.FormatFloat(want.Ratio, 'g', 4, 64)
		raw := strconv.FormatFloat(want.Ratio, 'g', -1, 64)
		if !strings.Contains(text.stdout, "ratio "+rounded+" ") {
			t.Fatalf("example %d: text %q lacks the ratio rounded to %s", i, text.stdout, rounded)
		}
		if raw != rounded && strings.Contains(text.stdout, raw) {
			t.Fatalf("example %d: text %q holds the unrounded ratio %s", i, text.stdout, raw)
		}
		if !strings.HasPrefix(text.stdout, string(strings.ToLower(strings.TrimPrefix(v.GetKind().String(), "VERDICT_KIND_")))+"\t") {
			t.Fatalf("example %d: text %q does not lead with the verdict %s", i, text.stdout, v.GetKind())
		}
	}
}

// A comparison that compared nothing, because the sides were measured under another task version
// or for another metric, says so in text instead of printing zeros that read as a measured zero.
// Every other verdict keeps its numbers. For any samples, the numbers appear exactly when the two
// sides were comparable.
func TestCompareOfSidesThatCannotBeComparedPrintsNoNumbers(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 11))
	samples := func() []float64 {
		out := make([]float64, 3+rng.IntN(5))
		level := 10 + rng.Float64()*1000
		for i := range out {
			out[i] = level * (1 + (rng.Float64()-0.5)*0.05)
		}
		return out
	}
	other := core.Metric{Name: "latency", Direction: core.Minimize}
	for i := range 30 {
		c := newResultsCLI(t)
		kind := i % 3 // 0: comparable, 1: task versions differ, 2: metrics differ
		candidate := storetest.Measurement{Commit: rev(1), Samples: samples(), At: c.clk.Now()}
		switch kind {
		case 1:
			candidate.TaskVersion = fmt.Sprintf("v%d", 2+rng.IntN(1000))
		case 2:
			candidate.Metric = other
		}
		storetest.SeedMeasurement(t, c.store, storetest.Measurement{Commit: rev(0), Samples: samples(), At: c.clk.Now()})
		storetest.SeedMeasurement(t, c.store, candidate)

		r := c.run(withTask("compare", "c1")...)
		mustExit(t, r, exitOK, "compare")
		numbers := strings.Contains(r.stdout, "ratio ") || strings.Contains(r.stdout, "mean ")
		if numbers != (kind == 0) {
			t.Fatalf("example %d (kind %d): text %q, numbers printed = %v", i, kind, r.stdout, numbers)
		}
		if kind != 0 && !strings.Contains(r.stdout, "not comparable") {
			t.Fatalf("example %d (kind %d): text %q does not say the sides are not comparable", i, kind, r.stdout)
		}
		if kind != 0 && !strings.HasPrefix(r.stdout, "inconclusive\t") {
			t.Fatalf("example %d (kind %d): text %q does not lead with the verdict", i, kind, r.stdout)
		}
	}
}

func TestCompareFailuresAreTypedAndMixedEnvironmentsAreRefused(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	r := c.run(withTask("compare", "c1", "--json")...)
	var f struct{ Kind string }
	if r.code != exitFailed || r.stdout != "" || json.Unmarshal([]byte(r.stderr), &f) != nil || f.Kind != "FAILURE_KIND_NOT_MEASURED" {
		t.Fatalf("compare of an unmeasured commit: %+v", r)
	}

	storetest.SeedMeasurement(t, c.store, storetest.Measurement{Commit: rev(1), Samples: win, At: c.clk.Now(), Environment: storetest.DockerEnvironment()})
	r = c.run(withTask("compare", "c1")...)
	if r.code != exitFailed || r.stdout != "" || !strings.Contains(r.stderr, "do not mix environments") {
		t.Fatalf("compare across environments: %+v", r)
	}
	r = c.run(withTask("promote", "c1")...)
	if r.code != exitFailed || !strings.Contains(r.stdout, "environment_mismatch:") {
		t.Fatalf("promote across environments: %+v", r)
	}
}

func TestCompareAgainstNamesTheBaseAndDefaultsToTheBranch(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	c.measured(1, win)
	c.measured(2, lose)

	byDefault := c.run(withTask("compare", "c1", "--json")...)
	named := c.run(withTask("compare", "c1", "--against", "base", "--json")...)
	mustExit(t, byDefault, exitOK)
	if byDefault.stdout != named.stdout {
		t.Fatalf("default %q, named %q", byDefault.stdout, named.stdout)
	}
	r := c.run(withTask("compare", "c2", "--against", "c1")...)
	mustExit(t, r, exitOK)
	if !strings.HasPrefix(r.stdout, "worse\t"+string(rev(2))[:12]+" vs "+string(rev(1))[:12]) {
		t.Fatalf("compare = %q", r.stdout)
	}
}

func TestResultReportsTheHeadAndItsNumberOrSaysThereIsNone(t *testing.T) {
	c := newResultsCLI(t)
	r := c.run(withTask("result")...)
	mustExit(t, r, exitFailed, "result before a promotion")
	if !strings.Contains(r.stdout, "no current result: nothing was promoted") || r.stderr != "" {
		t.Fatalf("result = %+v", r)
	}

	c.measured(0, level)
	picked := c.measured(1, []float64{118.123456789, 119, 121, 120, 119})
	mustExit(t, c.run(withTask("promote", "c1")...), exitOK)

	// Regression: the measurement the promotion rests on is the head's number right away.
	r = c.run(withTask("result")...)
	mustExit(t, r, exitOK, "result right after a promotion")
	if !strings.Contains(r.stdout, "head "+string(rev(1))[:12]) || !strings.Contains(r.stdout, string(picked.Job)) || !strings.Contains(r.stdout, "mean 119.4 over 5 samples") {
		t.Fatalf("result = %q", r.stdout)
	}
	j := c.run(withTask("result", "--json")...)
	var cur cliv1.CurrentResultResponse
	if err := protojson.Unmarshal([]byte(j.stdout), &cur); err != nil || cur.GetCurrent().GetResult().GetMeasure().GetSamples()[0] != 118.123456789 {
		t.Fatalf("result --json = %+v (%v): the samples must be exact", j, err)
	}
}

// noneReason is the reason `result --json` gives for having no number, read by its name.
func noneReason(t *testing.T, r result) string {
	t.Helper()
	var answer struct{ None struct{ Reason string } }
	if err := json.Unmarshal([]byte(r.stdout), &answer); err != nil {
		t.Fatalf("result --json = %+v (%v)", r, err)
	}
	return answer.None.Reason
}

// Regression: a result branch moved outside `promote`, as a task switch moves it, has a head of
// its own. Until that head is measured, `result` names the measure, with the head's full commit,
// that gives it its number; once it is, that measurement is the number.
func TestResultOfAHeadMovedOutsidePromoteIsItsOwnMeasurement(t *testing.T) {
	c := newResultsCLI(t)
	c.measured(0, level)
	c.measured(1, win)
	mustExit(t, c.run(withTask("promote", "c1")...), exitOK)
	c.world.Branches.AddBranch("/repo", resultBranch, rev(2))

	r := c.run(withTask("result")...)
	mustExit(t, r, exitFailed, "result at an unmeasured head")
	if want := "run `vibesys-cli submit measure " + string(rev(2)) + " --no-plan`"; !strings.Contains(r.stdout, want) || !strings.Contains(r.stdout, "head "+string(rev(2))[:12]) {
		t.Fatalf("result = %q, want it to say %q", r.stdout, want)
	}
	if got := noneReason(t, c.run(withTask("result", "--json")...)); got != "NO_CURRENT_RESULT_REASON_HEAD_NOT_MEASURED" {
		t.Fatalf("result --json reason = %s", got)
	}

	measured := c.measured(2, win)
	r = c.run(withTask("result")...)
	mustExit(t, r, exitOK, "result at a measured head")
	if !strings.Contains(r.stdout, "head "+string(rev(2))[:12]) || !strings.Contains(r.stdout, string(measured.Job)) {
		t.Fatalf("result = %q", r.stdout)
	}
}

func TestShowListsTheResultsOfACommit(t *testing.T) {
	c := newResultsCLI(t)
	r := c.run(withTask("show", "c1")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "no results for "+string(rev(1))[:12]) {
		t.Fatalf("show of an unmeasured commit: %+v", r)
	}
	check := storetest.SeedCheck(t, c.store, rev(1), c.clk.Now())
	m := c.measured(1, win)
	c.measured(2, level)

	r = c.run(withTask("show", "c1")...)
	mustExit(t, r, exitOK, "show c1")
	lines := strings.Split(strings.TrimSuffix(r.stdout, "\n"), "\n")
	if len(lines) != 2 || !strings.HasPrefix(lines[0], string(check.Job)+"\tcheck\tpassed") || !strings.HasPrefix(lines[1], string(m.Job)+"\tmeasure\tops (maximize)\tmean 120.4 over 5 samples\tcheck "+string(m.Job)) {
		t.Fatalf("show = %q", r.stdout)
	}
	var resp cliv1.ShowResponse
	j := c.run(withTask("show", "c1", "--json")...)
	if err := protojson.Unmarshal([]byte(j.stdout), &resp); err != nil || len(resp.GetResults()) != 2 {
		t.Fatalf("show --json = %+v (%v)", j, err)
	}
}

// Two measure jobs of one commit, a measure and its top-up, are one measurement: show prints one
// line with every sample, as compare counts them, and its JSON carries it pooled beside the
// results of each job.
func TestShowPoolsACommitsMeasureJobs(t *testing.T) {
	c := newResultsCLI(t)
	first := c.measured(1, []float64{100, 102})
	c.clk.Advance(time.Minute)
	topUp := c.measured(1, []float64{104})

	r := c.run(withTask("show", "c1")...)
	mustExit(t, r, exitOK, "show c1")
	lines := strings.Split(strings.TrimSuffix(r.stdout, "\n"), "\n")
	want := string(first.Job) + "," + string(topUp.Job) + "\tmeasure\tops (maximize)\tmean 102 over 3 samples\tcheck " + string(topUp.Job)
	if len(lines) != 1 || !strings.HasPrefix(lines[0], want) {
		t.Fatalf("show = %q, want one line starting %q", r.stdout, want)
	}
	var resp cliv1.ShowResponse
	j := c.run(withTask("show", "c1", "--json")...)
	if err := protojson.Unmarshal([]byte(j.stdout), &resp); err != nil || len(resp.GetResults()) != 2 || len(resp.GetMeasurements()) != 1 ||
		len(resp.GetMeasurements()[0].GetMeasure().GetSamples()) != 3 || resp.GetMeasurements()[0].GetMeasure().GetValue() != 102 {
		t.Fatalf("show --json = %+v (%v), want two results and one measurement of 3 samples, value 102", j, err)
	}
}

func TestResultCommandsRefuseBadArgumentsBeforeTheDaemon(t *testing.T) {
	c := newCLI(t)
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"show no commit", withTask("show"), "one commit"},
		{"show two", withTask("show", "a", "b"), "one commit"},
		{"compare no commit", withTask("compare"), "one commit"},
		{"compare bad flag", withTask("compare", "HEAD", "--nope"), "nope"},
		{"promote no commit", withTask("promote"), "one commit"},
		{"result extra", withTask("result", "HEAD"), "HEAD"},
		{"promote no task", []string{"promote", "HEAD"}, "--task"},
	}
	for _, tc := range cases {
		r := c.run(tc.args...)
		if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: %+v, want exit 2 naming %q", tc.name, r, tc.want)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid invocation started a daemon")
	}
}

// Two worktrees of one repository are one repository to the daemon: a job submitted in either is
// listed in both, and compare and promote in a linked worktree see the main worktree's results and
// move its result branch.
func TestTwoWorktreesOfARepositoryShareItsStore(t *testing.T) {
	c := newResultsCLI(t)
	c.git.AddWorktree("/worktrees/feature", "/repo", nil)
	c.measured(0, level)
	c.measured(1, win)
	inMain := c.submit("exit", "0")
	r := c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--repo", "/worktrees/feature/sub", "--", "exit", "0")...)
	mustExit(t, r, exitOK, "submit in the linked worktree")
	inLinked := strings.Fields(r.stdout)[0]
	for _, repo := range []string{"/repo", "/worktrees/feature"} {
		r := c.run(withTask("jobs", "--repo", repo)...)
		if r.code != exitOK || !strings.Contains(r.stdout, inMain) || !strings.Contains(r.stdout, inLinked) {
			t.Fatalf("jobs in %s = %+v, want both %s and %s", repo, r, inMain, inLinked)
		}
	}
	r = c.run(withTask("compare", "c1", "--repo", "/worktrees/feature")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "better") {
		t.Fatalf("compare in the linked worktree = %+v", r)
	}
	mustExit(t, c.run(withTask("promote", "c1", "--repo", "/worktrees/feature")...), exitOK, "promote in the linked worktree")
	if c.branchAt() != rev(1) {
		t.Fatalf("the main repository's branch is at %.7s", c.branchAt())
	}
}

// An agent works in its own linked worktree with VIBESYS_REPO naming the main checkout. A
// revision it names reads as its worktree has it: HEAD and HEAD~1 are the worktree's, from any
// directory in it, for show, compare, submit and promote alike, while a full commit and a branch
// name the same commit as anywhere. A caller outside the repository reads the main checkout, as
// before. Resolving HEAD in the main checkout would measure and promote a commit the agent never
// made.
func TestARevisionReadsAsTheCallersWorktreeHasIt(t *testing.T) {
	c := newResultsCLI(t)
	c.git.AddWorktree("/worktrees/agent", "/repo", map[string]core.Commit{"HEAD": rev(2), "@": rev(2), "HEAD~1": rev(1)})
	c.env["VIBESYS_REPO"] = "/repo"
	c.measured(0, level)
	inMain := c.measured(1, level)
	inAgent := c.measured(2, win)
	c.measured(4, lose)

	shows := map[string]struct {
		cwd, revision string
		want          string
	}{
		"HEAD in the worktree":                 {"/worktrees/agent", "HEAD", string(inAgent.Job)},
		"HEAD deeper in the worktree":          {"/worktrees/agent/sub/dir", "HEAD", string(inAgent.Job)},
		"@ in the worktree":                    {"/worktrees/agent", "@", string(inAgent.Job)},
		"HEAD~1 in the worktree":               {"/worktrees/agent", "HEAD~1", string(inMain.Job)},
		"a full commit in the worktree":        {"/worktrees/agent", string(rev(1)), string(inMain.Job)},
		"a branch in the worktree":             {"/worktrees/agent", "c1", string(inMain.Job)},
		"HEAD in the main checkout":            {"/repo", "HEAD", "no results for " + string(full)[:12]},
		"HEAD outside the repository":          {"/elsewhere", "HEAD", "no results for " + string(full)[:12]},
		"a branch outside the repository":      {"/elsewhere", "c2", string(inAgent.Job)},
		"a full commit outside the repository": {"/elsewhere", string(rev(4)), "mean 80.4 over 5 samples"},
	}
	for name, tc := range shows {
		c.cwd = tc.cwd
		r := c.run(withTask("show", tc.revision)...)
		if r.code != exitOK || !strings.Contains(r.stdout, tc.want) {
			t.Errorf("%s: show %s from %s = %+v, want %q", name, tc.revision, tc.cwd, r, tc.want)
		}
	}

	c.cwd = "/worktrees/agent"
	r := c.run(withTask("compare", "HEAD", "--against", "HEAD~1")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "better\t"+shortCommit(string(rev(2)))+" vs "+shortCommit(string(rev(1)))) {
		t.Fatalf("compare HEAD --against HEAD~1 in the worktree = %+v", r)
	}
	r = c.run(withTask("submit", "command", "--image", "harness", "HEAD", "--env", "host", "--", "exit", "0")...)
	mustExit(t, r, exitOK, "submit HEAD in the worktree")
	submitted := strings.Fields(r.stdout)[0]
	if listed := c.run(withTask("jobs", "--commit", "c2")...); !strings.Contains(listed.stdout, submitted) {
		t.Fatalf("submit HEAD in the worktree queued %s, not of the worktree's HEAD: jobs --commit c2 = %+v", submitted, listed)
	}
	mustExit(t, c.run(withTask("promote", "HEAD")...), exitOK, "promote HEAD in the worktree")
	if c.branchAt() != rev(2) {
		t.Fatalf("promote HEAD in the worktree moved the branch to %.7s, want the worktree's HEAD %.7s", c.branchAt(), rev(2))
	}
}

// A measurement's value is the number compare decides on: the daemon computes it, and the command
// prints it.
func TestAMeasurementsValueIsTheMeanCompareUses(t *testing.T) {
	c := newResultsCLI(t)
	samples := []float64{3, 1, 2, 10}
	c.measured(1, samples)
	r := c.run(withTask("show", "c1", "--json")...)
	var resp cliv1.ShowResponse
	if err := protojson.Unmarshal([]byte(r.stdout), &resp); err != nil || len(resp.GetResults()) != 1 {
		t.Fatalf("show --json = %+v (%v)", r, err)
	}
	if got := resp.GetResults()[0].GetMeasure().GetValue(); got != core.Headline(samples) || got != 4 {
		t.Fatalf("value = %v, want the mean %v", got, core.Headline(samples))
	}
	if text := c.run(withTask("show", "c1")...); !strings.Contains(text.stdout, "mean 4 over 4 samples") {
		t.Fatalf("show = %q lacks the value", text.stdout)
	}
}

// Placements are interchangeable, so measure jobs of one commit and image that ran on two
// placements pool into one measurement; show names where its samples were taken, each placement
// once, and where a check passed.
func TestShowSaysWhereTheSamplesWereTaken(t *testing.T) {
	c := newResultsCLI(t)
	on := func(placement string) core.Fingerprint {
		return core.Fingerprint{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, ImageDigest: "sha256:" + strings.Repeat("ab", 32),
			Facts: map[string]string{core.FactPlacement: placement, "os": "linux"}}
	}
	check := storetest.SeedCheckIn(t, c.store, rev(1), c.clk.Now(), on("b"), "v1")
	for _, p := range []string{"b", "a", "b"} {
		c.clk.Advance(time.Minute)
		storetest.SeedMeasurement(t, c.store, storetest.Measurement{Commit: rev(1), Samples: []float64{100, 102}, At: c.clk.Now(), Environment: on(p), CheckJob: check.Job})
	}
	r := c.run(withTask("show", "c1")...)
	mustExit(t, r, exitOK, "show c1")
	lines := strings.Split(strings.TrimSuffix(r.stdout, "\n"), "\n")
	if len(lines) != 2 || !strings.HasSuffix(lines[0], "\ton b") || !strings.Contains(lines[1], "over 6 samples") || !strings.HasSuffix(lines[1], "\ton b, a") {
		t.Fatalf("show = %q, want the check on b and one measurement of 6 samples on b, a", r.stdout)
	}
}
