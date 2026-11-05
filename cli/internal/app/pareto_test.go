package app_test

import (
	"context"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// The task of these tests has three objectives, as a serving benchmark does: throughput, scored
// and maximized, and two latencies, minimized.
var servingObjectives = core.Objectives{
	Metrics: []core.Metric{
		{Name: "tok_s", Direction: core.Maximize},
		{Name: "ttft", Direction: core.Minimize},
		{Name: "tpot", Direction: core.Minimize},
	},
	RelativeNoise: 0.01,
}

// measuredAt saves a measurement of a multi-objective commit at a point: the value of each
// objective, five samples of each, in the order of servingObjectives. It is taken in the host
// environment under the version of the task the rig's history has.
func (r *resultsRig) measuredAt(commit core.Commit, point ...float64) {
	r.t.Helper()
	r.measuredUnder(commit, r.taskVersion, storetest.HostEnvironment(), point...)
}

// measuredUnder is measuredAt under a task version and in an environment.
func (r *resultsRig) measuredUnder(commit core.Commit, version string, environment core.Fingerprint, point ...float64) {
	r.t.Helper()
	five := func(v float64) []float64 { return []float64{v, v, v, v, v} }
	var others []core.Observation
	for i, m := range servingObjectives.Metrics[1:] {
		others = append(others, core.Observation{Name: m.Name, Values: five(point[i+1])})
	}
	storetest.SeedMeasurement(r.t, r.store, storetest.Measurement{
		Commit: commit, Samples: five(point[0]), At: r.clock.Now(), Objectives: servingObjectives, Others: others,
		TaskVersion: version, Environment: environment,
	})
}

func (r *resultsRig) frontierCommits() []core.Commit {
	r.t.Helper()
	f, err := r.store.Frontier(context.Background())
	if err != nil {
		r.t.Fatal(err)
	}
	var out []core.Commit
	for _, m := range f.Members {
		out = append(out, m.Commit)
	}
	return out
}

func (r *resultsRig) addToFrontier(commit core.Commit) core.FrontierDecision {
	r.t.Helper()
	d, err := r.app.AddToFrontier(ctxFor(r.t), call, commit)
	if err != nil {
		r.t.Fatalf("AddToFrontier(%.7s): %v", commit, err)
	}
	return d
}

func frontierKinds(d core.FrontierDecision) []core.FrontierRefusalKind {
	refusal, _ := d.(core.FrontierRefusal)
	var out []core.FrontierRefusalKind
	for _, reason := range refusal.Reasons {
		out = append(out, reason.Kind)
	}
	return out
}

// Compare judges a multi-objective commit on every objective and says tradeoff when it is better on
// one and worse on another.
func TestCompareGivesAVerdictOnEveryObjective(t *testing.T) {
	r := newResultsRig(t)
	r.measuredAt(base, 100, 100, 100)
	r.measuredAt(cm(1), 200, 100, 200)
	got, err := r.app.Compare(ctxFor(t), call, cm(1), "")
	if err != nil {
		t.Fatal(err)
	}
	var kinds []core.VerdictKind
	for _, o := range got.Objectives {
		kinds = append(kinds, o.Verdict.Kind)
	}
	if got.Verdict.Kind != core.Tradeoff || !slices.Equal(kinds, []core.VerdictKind{core.Better, core.WithinNoise, core.Worse}) {
		t.Fatalf("comparison %+v", got)
	}
}

// A tradeoff is no promotion: the branch and the frontier stay as they were. A commit better on
// every objective is promoted and joins the frontier, evicting what it beats.
func TestParetoPromotionMovesTheBranchOnlyOnAWinAndKeepsTheFrontier(t *testing.T) {
	r := newResultsRig(t)
	r.measuredAt(base, 100, 100, 100)
	r.measuredAt(cm(1), 200, 100, 200)
	d, frontier, err := r.app.Promote(ctxFor(t), call, cm(1))
	if err != nil {
		t.Fatal(err)
	}
	if kinds := refusalKinds(t, d); !slices.Equal(kinds, []core.PromoteRefusalKind{core.RefusalTradeoff}) || frontier != nil {
		t.Fatalf("a tradeoff: %v, frontier %+v; want a tradeoff refusal and no frontier decision", kinds, frontier)
	}
	if r.head() != base || len(r.recorded()) != 0 || r.frontierCommits() != nil {
		t.Fatal("a refused promotion changed the branch, the record or the frontier")
	}

	if got := r.addToFrontier(base); frontierKinds(got) != nil {
		t.Fatalf("the head was refused by an empty frontier: %+v", got)
	}
	r.measuredAt(cm(2), 200, 90, 100)
	d, frontier, err = r.app.Promote(ctxFor(t), call, cm(2))
	if err != nil {
		t.Fatal(err)
	}
	p, ok := d.(core.Approved)
	if !ok || len(p.Promotion().Objectives) != 3 {
		t.Fatalf("a win on every objective: %+v, want a promotion with its objectives", d)
	}
	admission, ok := frontier.(core.FrontierAdmission)
	if !ok || !slices.Equal(admission.Evicted(), []core.Commit{base}) {
		t.Fatalf("frontier decision %+v, want an admission that evicts the old head", frontier)
	}
	if r.head() != cm(2) || !slices.Equal(r.frontierCommits(), []core.Commit{cm(2)}) {
		t.Fatalf("branch at %.7s, frontier %v", r.head(), r.frontierCommits())
	}
}

// A task switch moves the result branch to a commit with a new task, whose version builds another
// image. The base and the candidates are measured again under it, and a commit promoted then joins
// the frontier: the members measured only under the old version leave it rather than refuse it as
// measured in another environment, so the frontier can hold the current result.
func TestATaskSwitchStartsTheFrontierAfresh(t *testing.T) {
	r := newResultsRig(t)
	r.measuredAt(cm(1), 200, 100, 100)
	if got := r.addToFrontier(cm(1)); frontierKinds(got) != nil {
		t.Fatalf("an empty frontier refused %.7s: %+v", cm(1), got)
	}

	switched := tasktest.Files("task")
	switched[task.TasksDir+"/task/bench.sh"] = task.File{Data: []byte("#!/bin/sh\necho fixed\n"), Executable: true}
	for _, c := range []core.Commit{cm(2), cm(3)} {
		r.tasks.Put("/repo", c, switched)
	}
	tk, err := r.tasks.Task(ctxFor(t), "/repo", cm(2), "task")
	if err != nil || tk.Version == r.taskVersion {
		t.Fatalf("the switched task has version %q (%v), want a new one", tk.Version, err)
	}
	if err := r.branches.FastForward(ctxFor(t), "/repo", resultBranch, base, cm(2)); err != nil {
		t.Fatal(err)
	}
	r.measuredUnder(cm(2), tk.Version, storetest.DockerEnvironment(), 100, 100, 100)
	r.measuredUnder(cm(3), tk.Version, storetest.DockerEnvironment(), 300, 50, 50)

	d, frontier, err := r.app.Promote(ctxFor(t), call, cm(3))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := d.(core.Approved); !ok {
		t.Fatalf("a win on every objective under the new task: %+v, want a promotion", d)
	}
	if a, ok := frontier.(core.FrontierAdmission); !ok || a.Added().Commit != cm(3) {
		t.Fatalf("frontier decision %+v, want %.7s admitted", frontier, cm(3))
	}
	if got := r.frontierCommits(); !slices.Equal(got, []core.Commit{cm(3)}) {
		t.Fatalf("frontier %v, want only the commit of the new task", got)
	}

	// A commit measured only under the old task is not compared with the new one's members.
	r.measuredAt(side, 400, 10, 10)
	if kinds := frontierKinds(r.addToFrontier(side)); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierStale}) {
		t.Fatalf("a commit of the old task: %v, want stale", kinds)
	}
}

// A promoted commit that a member beats is promoted all the same, and the frontier says why it did
// not take it.
func TestAPromotionStandsWhenTheFrontierRefusesTheCommit(t *testing.T) {
	r := newResultsRig(t)
	r.measuredAt(base, 100, 100, 100)
	r.measuredAt(side, 400, 50, 50)
	r.addToFrontier(side)
	r.measuredAt(cm(1), 200, 100, 100)
	d, frontier, err := r.app.Promote(ctxFor(t), call, cm(1))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := d.(core.Approved); !ok {
		t.Fatalf("%+v, want a promotion", d)
	}
	if kinds := frontierKinds(frontier); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierDominated}) {
		t.Fatalf("frontier %+v, want dominated", frontier)
	}
	if r.head() != cm(1) || !slices.Equal(r.frontierCommits(), []core.Commit{side}) {
		t.Fatalf("branch at %.7s, frontier %v", r.head(), r.frontierCommits())
	}
}

// frontier add admits a tradeoff beside the members, refuses a commit a member beats or matches
// and names the member, and admits a member again with nothing changed.
func TestFrontierAddKeepsOnlyCommitsNoMemberBeats(t *testing.T) {
	r := newResultsRig(t)
	r.measuredAt(cm(1), 200, 100, 100)
	r.measuredAt(cm(2), 100, 50, 100)
	r.measuredAt(cm(3), 150, 100, 100)
	r.measuredAt(cm(4), 200, 100, 100)
	r.addToFrontier(cm(1))
	r.addToFrontier(cm(2))
	if got := r.frontierCommits(); !slices.Equal(got, []core.Commit{cm(1), cm(2)}) {
		t.Fatalf("frontier %v, want the two tradeoffs", got)
	}
	d := r.addToFrontier(cm(3))
	if kinds := frontierKinds(d); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierDominated}) || d.(core.FrontierRefusal).Reasons[0].Member != cm(1) {
		t.Fatalf("%+v, want dominated by the first member", d)
	}
	if kinds := frontierKinds(r.addToFrontier(cm(4))); !slices.Equal(kinds, []core.FrontierRefusalKind{core.FrontierWithinNoise}) {
		t.Fatalf("%v, want within_noise", kinds)
	}
	if a, ok := r.addToFrontier(cm(2)).(core.FrontierAdmission); !ok || !a.AlreadyMember() {
		t.Fatalf("%+v, want a member admitted again", a)
	}
	if got := r.frontierCommits(); !slices.Equal(got, []core.Commit{cm(1), cm(2)}) {
		t.Fatalf("refusals changed the frontier to %v", got)
	}

	entries, err := r.app.Frontier(ctxFor(t), call)
	if err != nil {
		t.Fatal(err)
	}
	if len(entries) != 2 || entries[1].Member.Commit != cm(2) || len(entries[1].Values) != 3 ||
		entries[1].Values[1] != (core.ObjectiveValue{Metric: servingObjectives.Metrics[1], Value: 50, Samples: 5}) {
		t.Fatalf("entries %+v", entries)
	}
}

// recordFails is a store whose next RecordPromotion fails before it records anything, as a daemon
// that died between offering the frontier the commit and recording the promotion.
type recordFails struct {
	store.Store
	armed *bool
}

func (f recordFails) RecordPromotion(ctx context.Context, p core.Approved) error {
	if *f.armed {
		*f.armed = false
		return &core.Failure{Kind: core.FailureInternal, Message: "cut off"}
	}
	return f.Store.RecordPromotion(ctx, p)
}

type recordFailsStores struct {
	app.Stores
	armed *bool
}

func (f recordFailsStores) Find(ctx context.Context, s core.Scope) (store.Store, error) {
	st, err := f.Stores.Find(ctx, s)
	return recordFails{st, f.armed}, err
}

func (f recordFailsStores) Create(ctx context.Context, s core.Scope) (store.Store, error) {
	st, err := f.Stores.Create(ctx, s)
	return recordFails{st, f.armed}, err
}

// The frontier takes a promoted commit before the promotion is recorded. A promotion cut off
// between the two leaves a frontier that is right, and asking again promotes the commit and finds it
// a member already.
func TestAPromotionCutOffAfterTheFrontierIsCompletedByAskingAgain(t *testing.T) {
	armed := true
	r := newResultsRigOver(t, newWorld(t), newBranches(), func(s app.Stores) app.Stores { return recordFailsStores{s, &armed} })
	r.measuredAt(base, 100, 100, 100)
	r.measuredAt(cm(1), 200, 90, 100)
	if _, _, err := r.app.Promote(ctxFor(t), call, cm(1)); err == nil {
		t.Fatal("the cut-off promotion succeeded")
	}
	if r.head() != base || len(r.recorded()) != 0 || !slices.Equal(r.frontierCommits(), []core.Commit{cm(1)}) {
		t.Fatalf("after the cut: branch %.7s, record %v, frontier %v", r.head(), r.recorded(), r.frontierCommits())
	}
	d, frontier, err := r.app.Promote(ctxFor(t), call, cm(1))
	if err != nil {
		t.Fatal(err)
	}
	if _, ok := d.(core.Approved); !ok {
		t.Fatalf("%+v, want a promotion", d)
	}
	if a, ok := frontier.(core.FrontierAdmission); !ok || !a.AlreadyMember() {
		t.Fatalf("frontier %+v, want the commit a member already", frontier)
	}
	if r.head() != cm(1) || !slices.Equal(r.frontierCommits(), []core.Commit{cm(1)}) {
		t.Fatalf("branch at %.7s, frontier %v", r.head(), r.frontierCommits())
	}
}

// Regression: a single-metric task behaves as it did. Its promotion offers nothing to a frontier,
// and its commits cannot join one.
func TestASingleMetricTaskHasNoFrontier(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	d, frontier, err := r.app.Promote(ctxFor(t), call, cm(1))
	if err != nil {
		t.Fatal(err)
	}
	if p, ok := d.(core.Approved); !ok || len(p.Promotion().Objectives) != 0 || frontier != nil {
		t.Fatalf("%+v, frontier %+v; want a plain promotion and no frontier decision", d, frontier)
	}
	if r.frontierCommits() != nil {
		t.Fatal("a single-metric promotion changed the frontier")
	}
	_, err = r.app.AddToFrontier(ctxFor(t), call, cm(1))
	wantFailure(t, "a single-metric commit", err, core.FailureInvalidInput)
	if entries, err := r.app.Frontier(ctxFor(t), call); err != nil || len(entries) != 0 {
		t.Fatalf("Frontier = %+v, %v", entries, err)
	}
}
