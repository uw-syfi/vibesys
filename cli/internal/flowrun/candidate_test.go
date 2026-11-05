package flowrun_test

import (
	"context"
	"errors"
	"io"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

const (
	probePause = 7 * time.Second
	startup    = 3 * time.Minute
)

// twoImages is the shell on a fake docker environment that builds candidates and serves them,
// with timers the test fires.
type twoImages struct {
	*fakeRig
	gates  *fakeenv.MemoryGates
	timers *startupTimers
}

// startupTimers fire every pause between two probes at once, so whether a server is ready decides
// a run and never how long it took, and every server's startup timeout when the test says so.
// No other timeout fires.
type startupTimers struct {
	once    sync.Once
	startup chan struct{}
}

func (s *startupTimers) Timeout(_ context.Context, d time.Duration) (<-chan struct{}, func()) {
	switch d {
	case probePause:
		c := make(chan struct{})
		close(c)
		return c, func() {}
	case startup:
		return s.startup, func() {}
	}
	return make(chan struct{}), func() {}
}

// expire ends the startup timeout of every server.
func (s *startupTimers) expire() { s.once.Do(func() { close(s.startup) }) }

func newTwoImages(t *testing.T, opts ...fakeenv.Option) *twoImages {
	t.Helper()
	files := hostfs.NewMemory()
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	system := cpu.NewFakeSystem(facts)
	gates := fakeenv.NewMemoryGates()
	timers := &startupTimers{startup: make(chan struct{})}
	opts = append([]fakeenv.Option{
		fakeenv.WithFiles(files), fakeenv.WithMachine(cpu.New(system, clk)), fakeenv.WithTimers(timers), fakeenv.WithProbePause(probePause),
	}, opts...)
	return &twoImages{
		fakeRig: &fakeRig{clock: clk, files: files, system: system, env: fakeenv.New(core.EnvironmentDocker, gates, opts...), checkouts: &fakeenv.Checkouts{}},
		gates:   gates, timers: timers,
	}
}

var serveBuild = core.CandidateBuild{Dockerfile: "serve/Dockerfile", Context: "serve"}

// plan is the scenario's plan for a task whose candidate serves with the script serve.
func candidatePlan(s scenario, serve ...string) flow.Plan {
	p := s.plan(false)
	p.Candidate = &task.Candidate{Build: serveBuild, Serve: serve, Ready: "/health", StartupTimeout: startup}
	return p
}

// run runs the plan to its verdict.
func (r *twoImages) run(ctx context.Context, t *testing.T, p flow.Plan) (flow.Verdict, string) {
	t.Helper()
	var (
		out strings.Builder
		mu  sync.Mutex
	)
	v, err := flowrun.Run(ctx, r.config(writerFunc(func(b []byte) (int, error) {
		mu.Lock()
		defer mu.Unlock()
		return out.Write(b)
	})), p)
	if err != nil {
		t.Fatal(err)
	}
	mu.Lock()
	defer mu.Unlock()
	return v, out.String()
}

type writerFunc func([]byte) (int, error)

func (f writerFunc) Write(b []byte) (int, error) { return f(b) }

var _ io.Writer = writerFunc(nil)

func (r *twoImages) noLeaks(t *testing.T) {
	t.Helper()
	if left := r.leaks(); len(left) > 0 {
		t.Fatalf("left %v", left)
	}
}

func TestAJobOfTwoImagesBuildsTheCandidateAndRunsEachCommandBesideItsServer(t *testing.T) {
	r := newTwoImages(t)
	p := candidatePlan(scenarios()[2], "print", "serving on ${PORT}", ";", "ready", ";", "hold", "forever")
	v, out := r.run(context.Background(), t, p)
	passed, ok := v.(flow.Passed)
	if !ok {
		t.Fatalf("verdict %+v\n%s", v, out)
	}
	built := r.env.CandidatesBuilt()
	if len(built) != 1 || built[0].Dockerfile != serveBuild.Dockerfile {
		t.Fatalf("built %+v, want the candidate once", built)
	}
	if !strings.HasPrefix(passed.Findings.CandidateImage, "sha256:") {
		t.Fatalf("findings %+v record no candidate image", passed.Findings)
	}
	if !strings.Contains(out, "serving on 18080") || !strings.Contains(out, "[candidate]") {
		t.Fatalf("output %q", out)
	}
	for _, x := range r.env.Runs() {
		if x.Candidate == nil || x.Candidate.Image != passed.Findings.CandidateImage {
			t.Fatalf("a command ran without the candidate: %+v", x)
		}
	}
	r.noLeaks(t)
}

func TestACandidateThatCannotBeBuiltRunsNothing(t *testing.T) {
	r := newTwoImages(t, fakeenv.WithCandidates(func(core.CandidateBuild) (core.CandidateImage, error) {
		return core.CandidateImage{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "build failed: step 3: pip install: no such package"}
	}))
	v, _ := r.run(context.Background(), t, candidatePlan(scenarios()[2], "ready"))
	na, ok := v.(flow.NotAttempted)
	if !ok || na.Stage != flow.StageCandidate || !strings.Contains(na.Reason, "no such package") {
		t.Fatalf("verdict %+v", v)
	}
	if runs := r.env.Runs(); len(runs) != 0 {
		t.Fatalf("ran %+v", runs)
	}
	r.noLeaks(t)
}

func TestAServerThatIsNeverReadyStopsTheJobWithItsLog(t *testing.T) {
	r := newTwoImages(t)
	p := candidatePlan(scenarios()[0], "print", "loading weights", ";", "mark", "loading", ";", "hold", "never")
	var (
		v  flow.Verdict
		wg sync.WaitGroup
	)
	wg.Go(func() { v, _ = r.run(context.Background(), t, p) })
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	defer cancel()
	if !r.gates.Reached(ctx, "loading") {
		t.Fatal("the server never started")
	}
	r.timers.expire()
	wg.Wait()
	stopped, ok := v.(flow.Stopped)
	if !ok || stopped.Exec.Kind != core.ExecServiceNotReady || stopped.Stage != flow.StageCheck || !strings.Contains(stopped.Reason, "loading weights") {
		t.Fatalf("verdict %+v", v)
	}
	r.noLeaks(t)
}

func TestACancelDuringTheLoadStopsTheServerAndTheCommand(t *testing.T) {
	r := newTwoImages(t)
	s := scenarios()[0]
	s.check = prog{fake: []string{"mark", "load", ";", "hold", "never"}}
	p := candidatePlan(s, "ready", ";", "hold", "forever")
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var (
		v  flow.Verdict
		wg sync.WaitGroup
	)
	wg.Go(func() { v, _ = r.run(ctx, t, p) })
	wait, done := context.WithTimeout(context.Background(), backstop)
	defer done()
	if !r.gates.Reached(wait, "load") {
		t.Fatal("the load never started")
	}
	cancel()
	wg.Wait()
	if stopped, ok := v.(flow.Stopped); !ok || !stopped.Cancelled() {
		t.Fatalf("verdict %+v", v)
	}
	r.noLeaks(t)
}

func TestAnEnvironmentWithoutImagesRunsNoTaskOfTwoImages(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	v, err := flowrun.Run(context.Background(), r.config(io.Discard), candidatePlan(scenarios()[0], "ready"))
	if err != nil {
		t.Fatal(err)
	}
	na, ok := v.(flow.NotAttempted)
	if !ok || na.Stage != flow.StageCandidate {
		t.Fatalf("verdict %+v", v)
	}
	var f *core.Failure
	if !errors.As(&na.Failure, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("failure %+v", na.Failure)
	}
}
