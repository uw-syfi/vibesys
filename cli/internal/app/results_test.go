package app_test

import (
	"context"
	"errors"
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// The history of the repository the result tests use: base, then a line of commits on it, and a
// side line that branches off base. The result branch of the task starts at base.
const resultBranch = "vibesys/result/task"

// cm is a commit whose hash is one digit repeated, so a failure message names it at a glance.
func cm(n int) core.Commit { return core.Commit(strings.Repeat(fmt.Sprintf("%x", n+1), 40)) }

var (
	base = cm(0)
	// line is cm(1) on base, cm(2) on cm(1), and so on up to cm(5).
	// side is cm(9), which branches off base and is on no line.
	side = cm(9)

	win    = []float64{120, 121, 119, 122, 120}
	level  = []float64{100, 101, 99, 100, 102}
	same   = []float64{101, 100, 102, 99, 100}
	lose   = []float64{80, 81, 79, 80, 82}
	tooFew = []float64{130, 131}
)

// resultsRig is a daemon over fakes with the repository's history and a result branch at base.
type resultsRig struct {
	*world
	branches *gitrepo.FakeBranches
	app      *app.App
	store    store.Store
	// taskVersion is the version of the task every commit of the history has.
	taskVersion string
}

func newBranches() *gitrepo.FakeBranches {
	b := gitrepo.NewFakeBranches()
	b.AddCommit("/repo", base)
	for n := 1; n <= 5; n++ {
		b.AddCommit("/repo", cm(n), cm(n-1))
	}
	b.AddCommit("/repo", side, base)
	b.AddBranch("/repo", resultBranch, base)
	return b
}

func newResultsRig(t *testing.T) *resultsRig {
	t.Helper()
	return newResultsRigOver(t, newWorld(t), newBranches(), nil)
}

// newResultsRigOver starts a daemon on the world. Stores wraps the world's stores when given.
func newResultsRigOver(t *testing.T, w *world, b *gitrepo.FakeBranches, wrap func(app.Stores) app.Stores) *resultsRig {
	t.Helper()
	return newResultsRigWith(t, w, b, wrap, nil)
}

// newResultsRigWith is newResultsRigOver with the repository seen through wrapBranches when given.
func newResultsRigWith(t *testing.T, w *world, b *gitrepo.FakeBranches, wrap func(app.Stores) app.Stores, wrapBranches func(gitrepo.Branches) gitrepo.Branches) *resultsRig {
	t.Helper()
	var seen gitrepo.Branches = b
	if wrapBranches != nil {
		seen = wrapBranches(b)
	}
	var stores app.Stores = w.stores
	if wrap != nil {
		stores = wrap(stores)
	}
	for _, c := range []core.Commit{base, cm(1), cm(2), cm(3), cm(4), cm(5), side} {
		w.tasks.Put("/repo", c, tasktest.Files("task"))
	}
	tk, err := w.tasks.Task(context.Background(), "/repo", base, "task")
	if err != nil {
		t.Fatal(err)
	}
	a := app.New(app.Config{
		Clock:        w.clock,
		Stores:       stores,
		Environments: fakeenv.Environments{core.EnvironmentHost: w.env},
		Supervisors:  w.supervisors(),
		Tasks:        w.taskSource(),
		Branches:     seen,
		Logf:         func(format string, args ...any) { t.Logf("daemon: "+format, args...) },
	})
	w.apps = append(w.apps, a)
	if err := a.Start(context.Background()); err != nil {
		t.Fatalf("Start: %v", err)
	}
	st, err := w.stores.Create(context.Background(), scope)
	if err != nil {
		t.Fatal(err)
	}
	return &resultsRig{world: w, branches: b, app: a, store: st, taskVersion: tk.Version}
}

// measured saves a measurement of the commit, recorded now, resting on a check of its own.
func (r *resultsRig) measured(commit core.Commit, samples []float64) core.Result {
	r.t.Helper()
	return storetest.SeedMeasurement(r.t, r.store, storetest.Measurement{Commit: commit, Samples: samples, At: r.clock.Now()})
}

func (r *resultsRig) promote(commit core.Commit) (core.PromotionDecision, error) {
	r.t.Helper()
	d, _, err := r.app.Promote(ctxFor(r.t), call, commit)
	return d, err
}

// promoted promotes a commit that must be accepted.
func (r *resultsRig) promoted(commit core.Commit) core.Promotion {
	r.t.Helper()
	d, err := r.promote(commit)
	if err != nil {
		r.t.Fatalf("Promote(%.7s): %v", commit, err)
	}
	approved, ok := d.(core.Approved)
	if !ok {
		r.t.Fatalf("Promote(%.7s) = %+v, want a promotion", commit, d)
	}
	return approved.Promotion()
}

func (r *resultsRig) head() core.Commit {
	c, _ := r.branches.At("/repo", resultBranch)
	return c
}

func (r *resultsRig) recorded() []core.Promotion {
	r.t.Helper()
	ps, err := r.store.Promotions(context.Background())
	if err != nil {
		r.t.Fatal(err)
	}
	return ps
}

func refusalKinds(t *testing.T, d core.PromotionDecision) []core.PromoteRefusalKind {
	t.Helper()
	refusal, ok := d.(core.PromoteRefusal)
	if !ok {
		t.Fatalf("%+v, want a refusal", d)
	}
	var kinds []core.PromoteRefusalKind
	for _, reason := range refusal.Reasons {
		kinds = append(kinds, reason.Kind)
	}
	return kinds
}

func wantFailure(t *testing.T, what string, err error, kind core.FailureKind) *core.Failure {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != kind || f.Message == "" {
		t.Fatalf("%s: error = %v, want a failure of kind %s with a message", what, err, kind)
	}
	return f
}

// A commit that is better than the head is promoted: the promotion is recorded with the verdict
// it rests on, and the branch is at the commit.
func TestPromoteMovesTheBranchAndRecordsThePromotion(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	m := r.measured(cm(1), win)

	p := r.promoted(cm(1))
	if p.Commit != cm(1) || p.Verdict.Kind != core.Better || p.ResultJob != m.Job || !p.Environment.Equal(storetest.HostEnvironment()) {
		t.Fatalf("promotion = %+v", p)
	}
	if got := r.head(); got != cm(1) {
		t.Fatalf("the branch is at %.7s, want %.7s", got, cm(1))
	}
	if got := r.recorded(); len(got) != 1 || got[0].Commit != cm(1) || got[0].Verdict != p.Verdict {
		t.Fatalf("recorded = %+v", got)
	}
}

// The reasons a promotion is refused are the ones the measurements give, and a refusal changes
// nothing: not the branch, not the record.
func TestPromoteRefusesAndChangesNothing(t *testing.T) {
	cases := []struct {
		name  string
		setup func(r *resultsRig)
		want  []core.PromoteRefusalKind
	}{
		{"the commit is unmeasured", func(r *resultsRig) { r.measured(base, level) },
			[]core.PromoteRefusalKind{core.RefusalUnmeasured}},
		{"the head is unmeasured", func(r *resultsRig) { r.measured(cm(1), win) },
			[]core.PromoteRefusalKind{core.RefusalUnmeasured}},
		{"worse", func(r *resultsRig) { r.measured(base, level); r.measured(cm(1), lose) },
			[]core.PromoteRefusalKind{core.RefusalWorse}},
		{"within noise", func(r *resultsRig) { r.measured(base, level); r.measured(cm(1), same) },
			[]core.PromoteRefusalKind{core.RefusalWithinNoise}},
		{"too few samples", func(r *resultsRig) { r.measured(base, level); r.measured(cm(1), tooFew) },
			[]core.PromoteRefusalKind{core.RefusalInconclusive}},
		{"no passing check", func(r *resultsRig) {
			r.measured(base, level)
			storetest.SeedMeasurement(r.t, r.store, storetest.Measurement{Commit: cm(1), Samples: win, At: r.clock.Now(), CheckJob: "j-gone"})
		}, []core.PromoteRefusalKind{core.RefusalCheckMissing}},
		{"another environment", func(r *resultsRig) {
			r.measured(base, level)
			storetest.SeedMeasurement(r.t, r.store, storetest.Measurement{Commit: cm(1), Samples: win, At: r.clock.Now(), Environment: storetest.DockerEnvironment()})
		}, []core.PromoteRefusalKind{core.RefusalEnvironmentMismatch}},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			r := newResultsRig(t)
			tc.setup(r)
			d, err := r.promote(cm(1))
			if err != nil {
				t.Fatalf("Promote: %v, want a refusal as a normal answer", err)
			}
			if got := refusalKinds(t, d); !slices.Equal(got, tc.want) {
				t.Fatalf("reasons = %v, want %v", got, tc.want)
			}
			for _, reason := range d.(core.PromoteRefusal).Reasons {
				if reason.Message == "" {
					t.Errorf("reason %s has no message", reason.Kind)
				}
			}
			if r.head() != base || len(r.recorded()) != 0 {
				t.Fatalf("a refusal moved the branch to %.7s or recorded %+v", r.head(), r.recorded())
			}
		})
	}
}

// A refusal carries every reason with its evidence, not the first: here the commit is worse than
// the head, rests on no check, and was measured before the branch moved.
func TestARefusalCarriesAllReasonsWithTheirEvidence(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	storetest.SeedMeasurement(t, r.store, storetest.Measurement{Commit: cm(9), Samples: lose, At: r.clock.Now(), CheckJob: "j-gone"})
	r.measured(cm(1), win)
	r.promoted(cm(1))

	d, err := r.promote(side)
	if err != nil {
		t.Fatal(err)
	}
	if got, want := refusalKinds(t, d), []core.PromoteRefusalKind{core.RefusalCheckMissing, core.RefusalWorse, core.RefusalStale}; !slices.Equal(got, want) {
		t.Fatalf("reasons = %v, want %v", got, want)
	}
	for _, reason := range d.(core.PromoteRefusal).Reasons {
		switch reason.Kind {
		case core.RefusalWorse:
			if reason.Verdict == nil || reason.Verdict.Kind != core.Worse || reason.Verdict.Ratio >= 1 {
				t.Errorf("worse carries verdict %+v", reason.Verdict)
			}
		case core.RefusalStale:
			if reason.MeasuredAgainst != base || reason.CurrentHead != cm(1) {
				t.Errorf("stale carries measured against %.7s, head %.7s; want %.7s and %.7s", reason.MeasuredAgainst, reason.CurrentHead, base, cm(1))
			}
		}
	}
}

// The branch only fast-forwards. A better commit that does not contain the head cannot be
// promoted, and that is a failure the caller can read, found before anything is recorded.
func TestPromoteOfACommitThatDoesNotContainTheHeadFailsAndRecordsNothing(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.promoted(cm(1))
	r.measured(side, []float64{150, 151, 149, 152, 150})

	_, err := r.promote(side)
	f := wantFailure(t, "a side line", err, core.FailureConflict)
	if r.head() != cm(1) || len(r.recorded()) != 1 {
		t.Fatalf("branch at %.7s with %d promotions after: %s", r.head(), len(r.recorded()), f.Message)
	}
}

func TestPromoteWithoutAResultBranchSaysHowToMakeOne(t *testing.T) {
	w := newWorld(t)
	b := gitrepo.NewFakeBranches()
	b.AddCommit("/repo", base)
	r := newResultsRigOver(t, w, b, nil)
	r.measured(base, level)
	r.measured(cm(1), win)

	_, err := r.promote(cm(1))
	f := wantFailure(t, "no branch", err, core.FailureNotFound)
	if want := "git branch " + resultBranch; !strings.Contains(f.Message, want) {
		t.Fatalf("message %q does not say %q", f.Message, want)
	}
}

func TestInvalidCommitsAreInvalidInput(t *testing.T) {
	r := newResultsRig(t)
	ctx := ctxFor(t)
	for _, bad := range []core.Commit{"", "main", "ABCDEF", cm(1)[:39]} {
		_, _, err := r.app.Promote(ctx, call, bad)
		wantFailure(t, "Promote "+string(bad), err, core.FailureInvalidInput)
		_, err = r.app.Compare(ctx, call, bad, "")
		wantFailure(t, "Compare "+string(bad), err, core.FailureInvalidInput)
		_, err = r.app.Show(ctx, scope, bad)
		wantFailure(t, "Show "+string(bad), err, core.FailureInvalidInput)
	}
	_, err := r.app.Compare(ctx, call, cm(1), "main")
	wantFailure(t, "Compare against a revision", err, core.FailureInvalidInput)
}

// The order of a promotion is: record, then move. A move that fails leaves the record, and the
// branch catches up the next time the daemon looks at it, however it looks.
func TestAnInterruptedMoveIsCompletedByTheNextLook(t *testing.T) {
	looks := map[string]func(*resultsRig){
		"compare against the head": func(r *resultsRig) {
			if _, err := r.app.Compare(ctxFor(r.t), call, cm(1), ""); err != nil {
				r.t.Fatal(err)
			}
		},
		"result": func(r *resultsRig) {
			if _, err := r.app.CurrentResult(ctxFor(r.t), call); err != nil {
				r.t.Fatal(err)
			}
		},
		"another promotion": func(r *resultsRig) {
			if _, err := r.promote(cm(2)); err != nil {
				r.t.Fatal(err)
			}
		},
		"a daemon that restarts": func(r *resultsRig) { newResultsRigOver(r.t, r.world, r.branches, nil) },
	}
	for name, look := range looks {
		t.Run(name, func(t *testing.T) {
			r := newResultsRig(t)
			r.measured(base, level)
			r.measured(cm(1), win)
			r.branches.FailNextFastForward(&core.Failure{Kind: core.FailureInternal, Message: "git died"})

			_, err := r.promote(cm(1))
			f := wantFailure(t, "the move", err, core.FailureInternal)
			if !strings.Contains(f.Message, "recorded") || !strings.Contains(f.Message, "git died") {
				t.Errorf("message %q does not say what state is left and why", f.Message)
			}
			if got := r.recorded(); len(got) != 1 || got[0].Commit != cm(1) {
				t.Fatalf("recorded = %+v, want the promotion kept", got)
			}
			if r.head() != base {
				t.Fatalf("the branch moved to %.7s though the move failed", r.head())
			}

			look(r)
			if r.head() != cm(1) {
				t.Fatalf("the branch is at %.7s after the next look, want %.7s", r.head(), cm(1))
			}
			if got := r.recorded(); len(got) < 1 || got[0].Commit != cm(1) {
				t.Fatalf("recorded = %+v", got)
			}
		})
	}
}

// failingRecord is a Stores whose stores cannot record a promotion while fail is set: a process
// that died before the record was written.
type failingRecord struct {
	app.Stores
	fail *atomic.Bool
}

func (f failingRecord) Find(ctx context.Context, s core.Scope) (store.Store, error) {
	st, err := f.Stores.Find(ctx, s)
	return noRecord{Store: st, fail: f.fail}, err
}

func (f failingRecord) Create(ctx context.Context, s core.Scope) (store.Store, error) {
	st, err := f.Stores.Create(ctx, s)
	return noRecord{Store: st, fail: f.fail}, err
}

type noRecord struct {
	store.Store
	fail *atomic.Bool
}

func (n noRecord) RecordPromotion(ctx context.Context, p core.Approved) error {
	if n.fail.Load() {
		return &core.Failure{Kind: core.FailureInternal, Message: "the disk is gone"}
	}
	return n.Store.RecordPromotion(ctx, p)
}

// A promotion that dies before it is recorded changes nothing, so asking again is the same as
// asking once.
func TestAPromotionThatDiesBeforeTheRecordChangesNothingAndCanBeAskedAgain(t *testing.T) {
	fail := &atomic.Bool{}
	r := newResultsRigOver(t, newWorld(t), newBranches(), func(s app.Stores) app.Stores { return failingRecord{s, fail} })
	r.measured(base, level)
	r.measured(cm(1), win)

	fail.Store(true)
	_, err := r.promote(cm(1))
	wantFailure(t, "the record", err, core.FailureInternal)
	if r.head() != base || len(r.recorded()) != 0 {
		t.Fatalf("branch at %.7s, recorded %+v: nothing may have changed", r.head(), r.recorded())
	}
	fail.Store(false)
	r.promoted(cm(1))
	if r.head() != cm(1) || len(r.recorded()) != 1 {
		t.Fatalf("branch at %.7s, recorded %+v", r.head(), r.recorded())
	}
}

// Whatever the samples, a commit is promoted exactly when the comparison says better: the branch
// moves, and one promotion is recorded, if and only if the verdict is better, and a refusal names
// the verdict's kind with the verdict it rests on.
func TestPromotionFollowsTheVerdictForAnySamples(t *testing.T) {
	rng := rand.New(rand.NewPCG(7, 11))
	samples := func() []float64 {
		n := 1 + rng.IntN(8)
		level := 50 + rng.Float64()*100
		spread := rng.Float64() * 0.1 * level
		out := make([]float64, n)
		for i := range out {
			out[i] = level + (rng.Float64()-0.5)*2*spread
		}
		return out
	}
	kinds := map[core.VerdictKind]core.PromoteRefusalKind{
		core.Worse: core.RefusalWorse, core.WithinNoise: core.RefusalWithinNoise, core.Inconclusive: core.RefusalInconclusive,
	}
	for i := range 80 {
		r := newResultsRig(t)
		r.measured(base, samples())
		r.measured(cm(1), samples())
		cmp, err := r.app.Compare(ctxFor(t), call, cm(1), "")
		if err != nil {
			t.Fatal(err)
		}
		d, err := r.promote(cm(1))
		if err != nil {
			t.Fatalf("example %d: %v", i, err)
		}
		switch d := d.(type) {
		case core.Approved:
			if cmp.Verdict.Kind != core.Better || d.Promotion().Verdict != cmp.Verdict || r.head() != cm(1) || len(r.recorded()) != 1 {
				t.Fatalf("example %d: promoted on %+v with verdict %+v, branch at %.7s", i, cmp.Verdict, d.Promotion().Verdict, r.head())
			}
		case core.PromoteRefusal:
			if cmp.Verdict.Kind == core.Better || len(d.Reasons) != 1 || d.Reasons[0].Kind != kinds[cmp.Verdict.Kind] ||
				d.Reasons[0].Verdict == nil || *d.Reasons[0].Verdict != cmp.Verdict || r.head() != base || len(r.recorded()) != 0 {
				t.Fatalf("example %d: refused %+v for verdict %+v, branch at %.7s", i, d.Reasons, cmp.Verdict, r.head())
			}
		}
	}
}

// Promotions go one at a time: of many callers promoting one commit, exactly one moves the branch
// and records, and the rest are refused.
func TestParallelPromotionsOfOneCommitMakeOnePromotion(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)

	const callers = 8
	var wg sync.WaitGroup
	var promotedCount, refusedCount atomic.Int64
	for range callers {
		wg.Add(1)
		go func() {
			defer wg.Done()
			d, _, err := r.app.Promote(ctxFor(t), call, cm(1))
			switch {
			case err != nil:
				t.Errorf("Promote: %v", err)
			default:
				if _, ok := d.(core.Approved); ok {
					promotedCount.Add(1)
				} else {
					refusedCount.Add(1)
				}
			}
		}()
	}
	wg.Wait()
	if promotedCount.Load() != 1 || refusedCount.Load() != callers-1 || len(r.recorded()) != 1 || r.head() != cm(1) {
		t.Fatalf("promoted %d, refused %d, recorded %d, branch at %.7s", promotedCount.Load(), refusedCount.Load(), len(r.recorded()), r.head())
	}
}

func TestCompareDefaultsToTheResultBranchHead(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.measured(cm(2), lose)

	defaulted, err := r.app.Compare(ctxFor(t), call, cm(1), "")
	if err != nil {
		t.Fatal(err)
	}
	named, err := r.app.Compare(ctxFor(t), call, cm(1), base)
	if err != nil || !reflect.DeepEqual(defaulted, named) || defaulted.Against != base || defaulted.Verdict.Kind != core.Better {
		t.Fatalf("default %+v, named %+v, %v", defaulted, named, err)
	}
	both, err := r.app.Compare(ctxFor(t), call, cm(2), cm(1))
	if err != nil || both.Verdict.Kind != core.Worse {
		t.Fatalf("%+v, %v", both, err)
	}
	// The verdict keeps the raw numbers.
	if both.Verdict.Ratio == 0 || both.Verdict.CandidateMean == 0 {
		t.Fatalf("verdict %+v lost its numbers", both.Verdict)
	}
}

func TestCompareFailsForAnUnmeasuredCommitAndForMixedEnvironments(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	_, err := r.app.Compare(ctxFor(t), call, cm(1), "")
	wantFailure(t, "an unmeasured commit", err, core.FailureNotMeasured)

	storetest.SeedMeasurement(t, r.store, storetest.Measurement{Commit: cm(1), Samples: win, At: r.clock.Now(), Environment: storetest.DockerEnvironment()})
	_, err = r.app.Compare(ctxFor(t), call, cm(1), "")
	wantFailure(t, "mixed environments", err, core.FailureEnvironmentMismatch)
}

// currentResult is what `result` reports now.
func (r *resultsRig) currentResult() core.ResultAnswer {
	r.t.Helper()
	a, err := r.app.CurrentResult(ctxFor(r.t), call)
	if err != nil {
		r.t.Fatal(err)
	}
	return a
}

// wantNoResult fails unless `result` reports no number, for the reason, at the head.
func (r *resultsRig) wantNoResult(reason core.NoCurrentResultReason, head core.Commit) {
	r.t.Helper()
	got, ok := r.currentResult().(core.NoCurrentResult)
	if !ok || got.Reason != reason || got.Head != head {
		r.t.Fatalf("result = %+v, want none: %s at %.7s", got, reason, head)
	}
}

// wantResult fails unless `result` reports the measurement as the number of the head.
func (r *resultsRig) wantResult(head core.Commit, m core.Result) {
	r.t.Helper()
	got, ok := r.currentResult().(core.CurrentResult)
	if !ok || got.Head != head || got.Result.Job != m.Job {
		r.t.Fatalf("result = %+v, want the measurement %s of %.7s", got, m.Job, head)
	}
}

// Regression: right after `promote`, `result` said the head had no measurement taken since it
// moved, although the measurement the promotion rests on is a measurement of the head. It is the
// head's number from the moment of the promotion, on a check in its own job or on a check job's
// pass, and a later measurement of the head replaces it.
func TestResultIsTheMeasurementAPromotionRestsOn(t *testing.T) {
	r := newResultsRig(t)
	r.wantNoResult(core.NoHead, "")
	r.measured(base, level)
	picked := r.measured(cm(1), win)
	r.wantNoResult(core.NoHead, "")
	r.promoted(cm(1))
	r.wantResult(cm(1), picked)

	later := r.measured(cm(1), []float64{118, 119, 121, 120, 119})
	r.wantResult(cm(1), later)

	// A measurement of another commit says nothing about the head; the promotion of that commit
	// makes the measurement it rests on, on a check job's pass, the number.
	check := storetest.SeedCheck(t, r.store, cm(2), r.clock.Now())
	r.clock.Advance(time.Minute)
	onPass := storetest.SeedMeasurement(t, r.store, storetest.Measurement{Commit: cm(2), Samples: []float64{140, 141, 139, 142, 140}, At: r.clock.Now(), CheckJob: check.Job})
	r.wantResult(cm(1), later)
	r.promoted(cm(2))
	r.wantResult(cm(2), onPass)
}

// Regression: a run that switches task versions commits the task change on the result branch, so
// the branch moves outside promote. That commit is the head, and its own measurement, a first
// measure of that very commit under the new task in the environment it builds, is its number. A
// measurement of the old head does not stand for it.
func TestResultOfAHeadMovedByATaskSwitchIsItsOwnMeasurement(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.promoted(cm(1))

	switched := tasktest.Files("task")
	switched[task.TasksDir+"/task/bench.sh"] = task.File{Data: []byte("#!/bin/sh\necho fixed\n"), Executable: true}
	r.tasks.Put("/repo", cm(2), switched)
	tk, err := r.tasks.Task(ctxFor(t), "/repo", cm(2), "task")
	if err != nil || tk.Version == r.taskVersion {
		t.Fatalf("the switched task has version %q (%v), want a new one", tk.Version, err)
	}
	if err := r.branches.FastForward(ctxFor(t), "/repo", resultBranch, cm(1), cm(2)); err != nil {
		t.Fatal(err)
	}
	r.wantNoResult(core.HeadNotMeasured, cm(2))

	r.clock.Advance(time.Minute)
	first := storetest.SeedMeasurement(t, r.store, storetest.Measurement{
		Commit: cm(2), Samples: win, At: r.clock.Now(), TaskVersion: tk.Version, Environment: storetest.DockerEnvironment(),
	})
	r.wantResult(cm(2), first)
}

// A measurement that rests on no passing check of its own commit, task version and environment is
// no number for the head, whenever it was taken.
func TestResultNeedsAPassingCheckInTheMeasurementsEnvironment(t *testing.T) {
	r := newResultsRig(t)
	r.measured(base, level)
	r.measured(cm(1), win)
	r.promoted(cm(1))
	r.branches.AddBranch("/repo", resultBranch, cm(2))

	elsewhere := storetest.SeedCheck(t, r.store, cm(2), r.clock.Now())
	storetest.SeedMeasurement(t, r.store, storetest.Measurement{
		Commit: cm(2), Samples: win, At: r.clock.Now(), Environment: storetest.DockerEnvironment(), CheckJob: elsewhere.Job,
	})
	r.wantNoResult(core.HeadNotMeasured, cm(2))
}

func TestShowListsEveryResultOfTheCommitOldestFirst(t *testing.T) {
	r := newResultsRig(t)
	check := storetest.SeedCheck(t, r.store, cm(1), r.clock.Now())
	m1 := r.measured(cm(1), win)
	r.measured(cm(2), level)
	m2 := storetest.SeedMeasurement(t, r.store, storetest.Measurement{Commit: cm(1), Samples: lose, At: r.clock.Now(), CheckJob: check.Job})

	got, err := r.app.Show(ctxFor(t), scope, cm(1))
	if err != nil {
		t.Fatal(err)
	}
	var jobs []core.JobID
	for _, x := range got {
		jobs = append(jobs, x.Job)
	}
	if want := []core.JobID{check.Job, m1.Job, m2.Job}; !slices.Equal(jobs, want) {
		t.Fatalf("jobs = %v, want %v", jobs, want)
	}
	none, err := r.app.Show(ctxFor(t), scope, cm(3))
	if err != nil || len(none) != 0 {
		t.Fatalf("a commit with no results: %v, %v", none, err)
	}
}

// A daemon without a repository to ask says so, and not that nothing was found.
func TestWithoutBranchesTheBranchCommandsAreUnavailable(t *testing.T) {
	w := newWorld(t)
	a := w.newApp()
	if _, err := w.stores.Create(ctxFor(t), scope); err != nil {
		t.Fatal(err)
	}
	_, _, err := a.Promote(ctxFor(t), call, cm(1))
	wantFailure(t, "Promote", err, core.FailureUnavailable)
	_, err = a.Compare(ctxFor(t), call, cm(1), "")
	wantFailure(t, "Compare", err, core.FailureUnavailable)
}

// Only a submission makes a store, so a read of results in a task that has had no job finds none
// and makes none: a commit has no results, the task has no head, and a decision has nothing to rest
// on and is refused as not found.
func TestResultCommandsOnATaskWithNoStoreMakeNone(t *testing.T) {
	w := newWorld(t)
	r := newResultsRigOver(t, w, newBranches(), nil)
	other := call
	other.Scope = scopeAt("/repo", "nobody")
	otherScope := other.Scope
	ctx := ctxFor(t)
	if got, err := r.app.Show(ctx, otherScope, cm(1)); err != nil || len(got) != 0 {
		t.Fatalf("Show = %v, %v; want none", got, err)
	}
	answer, err := r.app.CurrentResult(ctx, other)
	if none, ok := answer.(core.NoCurrentResult); err != nil || !ok || none.Reason != core.NoHead {
		t.Fatalf("CurrentResult = %+v, %v; want no head", answer, err)
	}
	_, err = r.app.Compare(ctx, other, cm(1), cm(0))
	wantFailure(t, "Compare", err, core.FailureNotFound)
	_, _, err = r.app.Promote(ctx, other, cm(1))
	wantFailure(t, "Promote", err, core.FailureNotFound)
	known, err := w.stores.Known(ctx)
	if err != nil || slices.Contains(known, otherScope) {
		t.Fatalf("known stores = %v, %v; the reads must not have made one", known, err)
	}
}
