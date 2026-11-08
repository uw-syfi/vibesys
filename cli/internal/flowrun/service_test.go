package flowrun_test

import (
	"context"
	"io"
	"slices"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// up is a server that says its port, is ready, and runs until it is ended.
var up = []string{"print", "serving on ${PORT}", ";", "alive", "server", ";", "ready", ";", "hold", "forever"}

// servedPlan is a measure job of samples on a task whose job-scoped server runs serve, with the
// benchmark bench and the reset reset.
func servedPlan(samples uint32, serve, bench, reset []string) flow.Plan {
	s := scenarios()[2]
	s.samples = samples
	s.bench = prog{fake: bench}
	p := s.plan(false)
	p.Service = &task.Service{
		Serve: serve, Ready: "/health", StartupTimeout: startup,
		Reset: task.Command{Argv: reset, Timeout: time.Minute},
	}
	return p
}

var (
	benchOK = []string{"emit", goodJSON}
	resetOK = []string{"print", "reset done"}
)

// argvs are the commands the environment ran, each as one line.
func argvs(r *twoImages) []string {
	var out []string
	for _, x := range r.env.Runs() {
		out = append(out, strings.Join(x.Argv, " "))
	}
	return out
}

// A measure job starts its server once, after the check, runs every sample against it, and the
// reset before each sample after the first; it records which server each sample ran on, and ends
// the server with the job.
func TestAMeasureJobStartsItsServerOnceAndResetsItBeforeEachSampleThatReusesIt(t *testing.T) {
	r := newTwoImages(t)
	v, out := r.run(context.Background(), t, servedPlan(3, up, benchOK, resetOK))
	passed, ok := v.(flow.Passed)
	if !ok {
		t.Fatalf("verdict %+v\n%s", v, out)
	}
	served := r.env.Served()
	if len(served) != 1 {
		t.Fatalf("started %d servers, want one: %+v", len(served), served)
	}
	if s := served[0]; s.Ready != "/health" || s.StartupTimeout != startup || s.Lifetime < startup+3*(time.Minute+time.Minute) || !strings.HasPrefix(s.Checkout.Dir, "/fake-checkouts/") {
		t.Fatalf("the server started as %+v", s)
	}
	var kinds []string
	for _, a := range argvs(r) {
		switch {
		case strings.HasPrefix(a, "emit"):
			kinds = append(kinds, "sample")
		case strings.HasPrefix(a, "print reset"):
			kinds = append(kinds, "reset")
		default:
			kinds = append(kinds, "check")
		}
	}
	if got := strings.Join(kinds, " "); got != "check sample reset sample reset sample" {
		t.Fatalf("ran %s", got)
	}
	if got := passed.Findings.Services; !slices.Equal(got, []core.SampleService{core.SampleServiceStarted, core.SampleServiceReused, core.SampleServiceReused}) {
		t.Fatalf("services %v", got)
	}
	if !strings.Contains(out, "serving on 18080") || strings.Count(out, "[service]") < 2 || !strings.Contains(out, "reset done") {
		t.Fatalf("output %q", out)
	}
	r.serverEnded(t)
}

// serverEnded fails the test when a server outlives the job, or anything else of it does.
func (r *twoImages) serverEnded(t *testing.T) {
	t.Helper()
	if n := r.env.LiveServices(); n != 0 {
		t.Fatalf("%d servers outlived the job", n)
	}
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	defer cancel()
	if len(r.env.Served()) > 0 && !r.gates.Gone(ctx, "server") {
		t.Fatal("the server's process outlived the job")
	}
	r.noLeaks(t)
}

// Every way a job with a server ends, the server ends with it: a pass, a sample that fails, a
// reset that fails, a server that is never ready, one that exits under a sample, and a cancel
// while the server starts or while a sample runs.
func TestTheJobsServerEndsOnEveryPathTheJobEndsBy(t *testing.T) {
	cases := []struct {
		name                string
		serve, bench, reset []string
		// act ends the job from outside once it is under way, given the cancel of its context.
		act  func(t *testing.T, r *twoImages, cancel func())
		want func(v flow.Verdict) bool
	}{
		{
			name: "a pass", serve: up, bench: benchOK, reset: resetOK,
			want: func(v flow.Verdict) bool { _, ok := v.(flow.Passed); return ok },
		},
		{
			name: "a sample that fails", serve: up, bench: []string{"exit", "1"}, reset: resetOK,
			want: func(v flow.Verdict) bool { s, ok := v.(flow.Stopped); return ok && s.Stage == flow.StageBenchmark },
		},
		{
			name: "a reset that fails", serve: up, bench: benchOK, reset: []string{"print", "server busy", ";", "exit", "3"},
			want: func(v flow.Verdict) bool {
				s, ok := v.(flow.Stopped)
				return ok && s.Stage == flow.StageReset && s.Exec.ExitCode == 3 && strings.Contains(s.Reason, "before sample 2")
			},
		},
		{
			name: "a server that is never ready", serve: []string{"print", "loading weights", ";", "alive", "server", ";", "mark", "loading", ";", "hold", "forever"}, bench: benchOK, reset: resetOK,
			act: func(t *testing.T, r *twoImages, _ func()) { r.reach(t, "loading"); r.timers.expire() },
			want: func(v flow.Verdict) bool {
				s, ok := v.(flow.Stopped)
				return ok && s.Stage == flow.StageService && s.Exec.Kind == core.ExecServiceNotReady && strings.Contains(s.Reason, "loading weights")
			},
		},
		{
			name: "a server that exits under a sample", serve: []string{"alive", "server", ";", "ready", ";", "hold", "crash", ";", "print", "segfault", ";", "exit", "139"},
			bench: []string{"mark", "sampling", ";", "hold", "forever"}, reset: resetOK,
			act: func(t *testing.T, r *twoImages, _ func()) { r.reach(t, "sampling"); r.gates.Release("crash") },
			want: func(v flow.Verdict) bool {
				s, ok := v.(flow.Stopped)
				return ok && s.Stage == flow.StageBenchmark && s.Exec.Kind == core.ExecServiceExited && strings.Contains(s.Reason, "segfault")
			},
		},
		{
			name: "a cancel while the server starts", serve: []string{"alive", "server", ";", "mark", "loading", ";", "hold", "forever"}, bench: benchOK, reset: resetOK,
			act: func(t *testing.T, r *twoImages, cancel func()) { r.reach(t, "loading"); cancel() },
			want: func(v flow.Verdict) bool {
				s, ok := v.(flow.Stopped)
				return ok && s.Cancelled() && s.Stage == flow.StageService
			},
		},
		{
			name: "a cancel while a sample runs", serve: up, bench: []string{"mark", "sampling", ";", "hold", "forever"}, reset: resetOK,
			act: func(t *testing.T, r *twoImages, cancel func()) { r.reach(t, "sampling"); cancel() },
			want: func(v flow.Verdict) bool {
				s, ok := v.(flow.Stopped)
				return ok && s.Cancelled() && s.Stage == flow.StageBenchmark
			},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			r := newTwoImages(t)
			ctx, cancel := context.WithCancel(context.Background())
			defer cancel()
			var (
				v   flow.Verdict
				out string
				wg  sync.WaitGroup
			)
			wg.Go(func() { v, out = r.run(ctx, t, servedPlan(2, c.serve, c.bench, c.reset)) })
			if c.act != nil {
				c.act(t, r, cancel)
			}
			wg.Wait()
			if !c.want(v) {
				t.Fatalf("verdict %+v\n%s", v, out)
			}
			if len(r.env.Served()) != 1 {
				t.Fatalf("started %d servers, want one", len(r.env.Served()))
			}
			r.serverEnded(t)
		})
	}
}

func (r *twoImages) reach(t *testing.T, point string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	defer cancel()
	if !r.gates.Reached(ctx, point) {
		t.Fatalf("the job never reached %s", point)
	}
}

// A server that ended is not reused: asked whether it runs, the shell says it does not, and a
// start again ends what is left of it before it starts the next, which the job's end ends.
func TestAServerThatEndedIsStartedAgainAndTheEndedOneCleanedUp(t *testing.T) {
	r := newTwoImages(t)
	var out strings.Builder
	var mu sync.Mutex
	sh := flowrun.New(r.config(writerFunc(func(b []byte) (int, error) { mu.Lock(); defer mu.Unlock(); return out.Write(b) })))
	defer sh.Close()
	ctx := context.Background()
	if e, ok := sh.Do(ctx, flow.Checkout{}).(flow.CheckedOut); !ok {
		t.Fatalf("checkout %+v", e)
	}
	serve := flow.Serve{Argv: up, Ready: "/health", StartupTimeout: startup, Lifetime: time.Hour}
	if e := sh.Do(ctx, serve); e != (flow.Serving{}) {
		t.Fatalf("serve %+v", e)
	}
	if e := sh.Do(ctx, flow.ProbeService{}); e != (flow.ServiceState{Alive: true}) {
		t.Fatalf("a running server probed as %+v", e)
	}
	r.env.CrashServices()
	if e := sh.Do(ctx, flow.ProbeService{}); e != (flow.ServiceState{Alive: false}) {
		t.Fatalf("an ended server probed as %+v", e)
	}
	serve.Again = true
	if e := sh.Do(ctx, serve); e != (flow.Serving{}) {
		t.Fatalf("serve again %+v", e)
	}
	if n, live := len(r.env.Served()), r.env.LiveServices(); n != 2 || live != 1 {
		t.Fatalf("%d servers started and %d live, want 2 and the new one", n, live)
	}
	if e := sh.Do(ctx, flow.ProbeService{}); e != (flow.ServiceState{Alive: true}) {
		t.Fatalf("the new server probed as %+v", e)
	}
	sh.Close()
	mu.Lock()
	said := out.String()
	mu.Unlock()
	if !strings.Contains(said, "starting it again") {
		t.Fatalf("output %q", said)
	}
	r.serverEnded(t)
}

// A check job starts no server unless its check shares it; when it does, the check runs against it.
func TestACheckJobStartsTheServerOnlyWhenItsCheckSharesIt(t *testing.T) {
	for _, shared := range []bool{false, true} {
		r := newTwoImages(t)
		p := servedPlan(1, up, benchOK, resetOK)
		p.Kind, p.Samples, p.Benchmark, p.Result, p.CheckPass = core.JobCheck, 0, task.Command{}, task.Result{}, ""
		p.Service.ShareWithCheck = shared
		v, out := r.run(context.Background(), t, p)
		if _, ok := v.(flow.Passed); !ok {
			t.Fatalf("shared %v: verdict %+v\n%s", shared, v, out)
		}
		if n := len(r.env.Served()); (n == 1) != shared || n > 1 {
			t.Fatalf("shared %v: started %d servers", shared, n)
		}
		if n := len(r.env.Runs()); n != 1 {
			t.Fatalf("shared %v: ran %d commands, want the check", shared, n)
		}
		r.serverEnded(t)
	}
}

// An environment that runs no images runs no job-scoped server, and says so before any sample.
func TestAnEnvironmentWithoutImagesRunsNoJobScopedServer(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	v, err := flowrun.Run(context.Background(), r.config(io.Discard), servedPlan(2, up, benchOK, resetOK))
	if err != nil {
		t.Fatal(err)
	}
	na, ok := v.(flow.NotAttempted)
	if !ok || na.Stage != flow.StageService || na.Failure.Kind != core.FailureInvalidInput || !strings.Contains(na.Failure.Message, "[service]") {
		t.Fatalf("verdict %+v", v)
	}
	for _, x := range r.env.Runs() {
		if strings.HasPrefix(strings.Join(x.Argv, " "), "emit") {
			t.Fatalf("a sample ran: %+v", x)
		}
	}
	if left := r.leaks(); len(left) > 0 {
		t.Fatalf("left %v", left)
	}
}

// A task that does not declare [service] never has a server started for its job, whatever its
// job and however it ends: each of its commands starts what it needs, as before.
func TestATaskWithoutAServiceStartsNoJobServer(t *testing.T) {
	for _, s := range scenarios() {
		r := newTwoImages(t)
		p := s.plan(false)
		if p.Service != nil {
			t.Fatalf("%s: the scenario has a job's server", s.name)
		}
		r.run(context.Background(), t, p)
		if served := r.env.Served(); len(served) != 0 {
			t.Fatalf("%s: started %+v", s.name, served)
		}
	}
}
