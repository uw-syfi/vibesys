// Package suptest is the contract suite every supervisor.Supervisors implementation passes, so
// the daemon, written against the interface, is correct for the in-process one it is tested with
// and for the processes it runs with.
//
// An implementation is given to Run as a Rig. The suite drives commands with fakeenv scripts, so
// it needs the rig to let a script that waits at a gate go on (Rig.Open) and to kill a
// supervisor from outside (Rig.Kill).
package suptest

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"math/rand"
	"runtime"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/owned"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Backstop bounds every wait in the suite. It only stops a hung run from hanging the build: no
// test passes or fails by how long it took.
const Backstop = time.Minute

// Rig is one implementation under test, with the levers the suite needs.
type Rig struct {
	// Supervisors is the implementation, as the running daemon has it.
	Supervisors supervisor.Supervisors
	// Restart returns Supervisors as a restarted daemon has them: new, over the runs that
	// survived. The old Supervisors is not used again.
	Restart func() supervisor.Supervisors
	// Open lets a script that waits at the gate go on.
	Open func(gate string)
	// Kill ends the supervisor of a job without letting it report, as SIGKILL does.
	Kill func(ref lifecycle.Ref)
}

// Run runs the suite. newRig makes a fresh rig for each test and arranges for it to be closed
// when the test ends.
func Run(t *testing.T, newRig func(t *testing.T) Rig) {
	t.Helper()
	cases := []struct {
		name string
		fn   func(t *testing.T, r Rig)
	}{
		{"output and exit codes", testOutputAndExit},
		{"follow resumes from any offset", testFollowResumes},
		{"start is idempotent", testStartIdempotent},
		{"cancel ends a running command and keeps the reason", testCancel},
		{"cancel after the end changes nothing", testCancelAfterEnd},
		{"first cancel reason wins", testFirstReasonWins},
		{"a command that cannot be attempted says so", testNotAttempted},
		{"release refuses a running job and works after the end", testRelease},
		{"a parked supervisor can be released and never runs", testParkedRelease},
		{"a killed supervisor is gone", testKilled},
		{"a supervisor killed before its start proves its command never started", testKilledParked},
		{"output is numbered from the launch's output base", testOutputBase},
		{"a restarted daemon finds a running job and follows it", testRestartRunning},
		{"a restarted daemon finds an ended job's outcome", testRestartFinished},
		{"runs left over are found and forgotten", testRuns},
		{"closing a handle leaves the supervisor", testCloseLeavesTheSupervisor},
		{"a launch spawns once", testSpawnOnce},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) { c.fn(t, newRig(t)) })
	}
}

func launch(job string, script ...string) lifecycle.Launch {
	return lifecycle.Launch{
		Repository: "/repo", Task: "task", Job: core.JobID(job),
		Spec: core.JobSpec{
			Commit:      core.Commit("0123456789abcdef0123456789abcdef01234567"),
			Environment: core.EnvironmentHost,
			Settings:    core.CommandSettings{Image: core.ImageHarness, Argv: script, Timeout: time.Hour},
		},
	}
}

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), Backstop)
	t.Cleanup(cancel)
	return ctx
}

// follow collects a job's output and outcome from an offset.
func follow(ctx context.Context, h supervisor.Handle, offset uint64) ([]byte, lifecycle.Outcome, error) {
	var out []byte
	o, err := h.Follow(ctx, offset, func(data []byte) error {
		out = append(out, data...)
		return nil
	})
	return out, o, err
}

func start(t *testing.T, r Rig, l lifecycle.Launch) supervisor.Handle {
	t.Helper()
	ctx := ctxFor(t)
	h, err := r.Supervisors.Spawn(ctx, l)
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	if err := h.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	return h
}

// seen follows a job in the background and reports on a channel each time its output so far
// contains the text, so a test waits for what the job did and not for a time.
type seen struct {
	mu      sync.Mutex
	buf     []byte
	changed chan struct{}
	done    chan struct{}
	outcome lifecycle.Outcome
	err     error
}

func watch(ctx context.Context, h supervisor.Handle, offset uint64) *seen {
	s := &seen{changed: make(chan struct{}, 1), done: make(chan struct{})}
	var follower owned.Group // Joined through s.done, which a test can select on.
	follower.Go(func() {
		defer close(s.done)
		s.outcome, s.err = h.Follow(ctx, offset, func(data []byte) error {
			s.mu.Lock()
			s.buf = append(s.buf, data...)
			s.mu.Unlock()
			select {
			case s.changed <- struct{}{}:
			default:
			}
			return nil
		})
	})
	return s
}

func (s *seen) output() []byte {
	s.mu.Lock()
	defer s.mu.Unlock()
	return bytes.Clone(s.buf)
}

// until waits until the output so far contains text.
func (s *seen) until(t *testing.T, ctx context.Context, text string) {
	t.Helper()
	for !bytes.Contains(s.output(), []byte(text)) {
		select {
		case <-s.changed:
		case <-s.done:
			if !bytes.Contains(s.output(), []byte(text)) {
				t.Fatalf("the job ended without printing %q; output %q, error %v", text, s.output(), s.err)
			}
		case <-ctx.Done():
			t.Fatalf("waiting for %q: %v; output so far %q", text, ctx.Err(), s.output())
		}
	}
}

func (s *seen) finish(t *testing.T, ctx context.Context) lifecycle.Outcome {
	t.Helper()
	select {
	case <-s.done:
	case <-ctx.Done():
		t.Fatalf("waiting for the outcome: %v", ctx.Err())
	}
	if s.err != nil {
		t.Fatalf("Follow: %v", s.err)
	}
	return s.outcome
}

func testOutputAndExit(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	rng := rand.New(rand.NewSource(1))
	for i := 0; i < 8; i++ {
		code := rng.Intn(256)
		words := fmt.Sprintf("line-%d", i)
		h := start(t, r, launch(fmt.Sprintf("j%d", i), "print", words, ";", "print", "second", ";", "exit", fmt.Sprint(code)))
		got, o, err := follow(ctx, h, 0)
		if err != nil {
			t.Fatalf("Follow: %v", err)
		}
		if want := words + "\nsecond\n"; string(got) != want {
			t.Errorf("output = %q, want %q", got, want)
		}
		if notAttemptedOf(o) != nil || execOf(o) != (core.ExecOutcome{Kind: core.ExecExited, ExitCode: code}) || o.CancelAsked {
			t.Errorf("outcome = %+v, want exited %d", o, code)
		}
	}
}

func testFollowResumes(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "print", "alpha", ";", "print", "beta", ";", "print", "gamma"))
	all, _, err := follow(ctx, h, 0)
	if err != nil {
		t.Fatal(err)
	}
	// A reader that comes back with the offset it holds gets the rest, exactly once, and the
	// outcome again: this is how a restarted daemon resumes.
	for offset := 0; offset <= len(all); offset++ {
		got, o, err := follow(ctx, h, uint64(offset))
		if err != nil {
			t.Fatalf("offset %d: %v", offset, err)
		}
		if !bytes.Equal(got, all[offset:]) {
			t.Errorf("offset %d: got %q, want %q", offset, got, all[offset:])
		}
		if execOf(o).Kind != core.ExecExited {
			t.Errorf("offset %d: outcome %+v", offset, o)
		}
	}
}

func testStartIdempotent(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "print", "once"))
	for i := 0; i < 3; i++ {
		if err := h.Start(ctx); err != nil {
			t.Fatalf("Start #%d: %v", i+2, err)
		}
	}
	got, _, err := follow(ctx, h, 0)
	if err != nil || string(got) != "once\n" {
		t.Fatalf("output %q, error %v; want one run", got, err)
	}
	if err := h.Start(ctx); err != nil {
		t.Fatalf("Start after the end: %v", err)
	}
}

func testCancel(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "print", "started", ";", "hold", "g", ";", "print", "never"))
	w := watch(ctx, h, 0)
	w.until(t, ctx, "started")
	if err := h.Cancel(ctx, "because"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	o := w.finish(t, ctx)
	if execOf(o).Kind != core.ExecCancelled || !o.CancelAsked || o.CancelReason != "because" {
		t.Errorf("outcome = %+v, want cancelled because", o)
	}
	if bytes.Contains(w.output(), []byte("never")) {
		t.Errorf("the command went on after the cancel: %q", w.output())
	}
}

func testCancelAfterEnd(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "exit", "0"))
	if _, _, err := follow(ctx, h, 0); err != nil {
		t.Fatal(err)
	}
	if err := h.Cancel(ctx, "late"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	_, o, err := follow(ctx, h, 0)
	if err != nil {
		t.Fatal(err)
	}
	if o.CancelAsked || execOf(o) != (core.ExecOutcome{Kind: core.ExecExited}) {
		t.Errorf("outcome = %+v, want the exit it had", o)
	}
}

func testFirstReasonWins(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "print", "up", ";", "hold", "g"))
	w := watch(ctx, h, 0)
	w.until(t, ctx, "up")
	for _, reason := range []string{"first", "second", ""} {
		if err := h.Cancel(ctx, reason); err != nil {
			t.Fatalf("Cancel: %v", err)
		}
	}
	if o := w.finish(t, ctx); !o.CancelAsked || o.CancelReason != "first" {
		t.Errorf("outcome = %+v, want the first reason", o)
	}
}

func testNotAttempted(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "fail", "no such environment"))
	_, o, err := follow(ctx, h, 0)
	if err != nil {
		t.Fatal(err)
	}
	if notAttemptedOf(o) == nil || notAttemptedOf(o).Kind != core.FailureUnavailable || notAttemptedOf(o).Message != "no such environment" {
		t.Errorf("outcome = %+v, want not attempted: unavailable", o)
	}
}

func testRelease(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	h := start(t, r, launch("j1", "print", "up", ";", "hold", "g"))
	w := watch(ctx, h, 0)
	w.until(t, ctx, "up")
	var f *core.Failure
	if err := h.Release(ctx); !errors.As(err, &f) || f.Kind != core.FailureConflict {
		t.Fatalf("Release of a running job = %v, want a conflict", err)
	}
	r.Open("g")
	w.finish(t, ctx)
	if err := h.Release(ctx); err != nil {
		t.Fatalf("Release after the end: %v", err)
	}
	if err := h.Release(ctx); err != nil && !errors.Is(err, supervisor.ErrGone) {
		t.Fatalf("second Release: %v", err)
	}
	if _, _, err := follow(ctx, h, 0); !errors.Is(err, supervisor.ErrGone) {
		t.Fatalf("Follow after Release = %v, want ErrGone", err)
	}
}

func testParkedRelease(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "must not run")
	h, err := r.Supervisors.Spawn(ctx, l)
	if err != nil {
		t.Fatal(err)
	}
	if err := h.Release(ctx); err != nil {
		t.Fatalf("Release of a parked supervisor: %v", err)
	}
	if err := h.Start(ctx); err == nil {
		t.Fatal("Start after Release succeeded")
	}
	if err := r.Supervisors.Forget(ctx, l.Ref(), lifecycle.ProofReleased); err != nil {
		t.Fatalf("Forget: %v", err)
	}
}

func testKilled(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "up", ";", "hold", "g")
	h := start(t, r, l)
	w := watch(ctx, h, 0)
	w.until(t, ctx, "up")
	r.Kill(l.Ref())
	select {
	case <-w.done:
	case <-ctx.Done():
		t.Fatal("Follow did not return after the supervisor was killed")
	}
	// A process's connection can end before its lock does, so proof can take another look, and
	// only ctx bounds them.
	err := w.err
	var notYet *core.Failure
	for errors.As(err, &notYet) && ctx.Err() == nil {
		_, err = h.Follow(ctx, 1<<62, func([]byte) error { return nil })
	}
	if !errors.Is(err, supervisor.ErrGone) {
		t.Fatalf("Follow after a kill = %v, want ErrGone", err)
	}
	dead, ok := find(t, r.Supervisors, l.Ref()).(supervisor.Dead)
	if !ok {
		t.Fatalf("after a kill the run is %#v, want dead", find(t, r.Supervisors, l.Ref()))
	}
	if _, never := dead.Start.(lifecycle.NeverStarted); never {
		t.Fatal("a run killed after its start is said to have never started")
	}
	if err := r.Supervisors.Forget(ctx, l.Ref(), ""); err == nil {
		t.Fatal("Forget took no proof")
	}
	if err := r.Supervisors.Forget(ctx, l.Ref(), dead.Proof); err != nil {
		t.Fatalf("Forget: %v", err)
	}
	if len(all(t, r.Supervisors)) != 0 {
		t.Fatal("a forgotten run is still listed")
	}
}

// A supervisor killed while parked, never told to start, is proved dead with a command that never
// started, which is what lets a job run again whatever its kind.
func testKilledParked(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "never")
	h, err := r.Supervisors.Spawn(ctx, l)
	if err != nil {
		t.Fatalf("Spawn: %v", err)
	}
	defer h.Close()
	w := watch(ctx, h, 0)
	r.Kill(l.Ref())
	// Follow returns once the connection to the killed supervisor ends.
	select {
	case <-w.done:
	case <-ctx.Done():
		t.Fatal("Follow did not return after the supervisor was killed")
	}
	dead := awaitDead(t, ctx, r.Supervisors, l.Ref())
	if _, never := dead.Start.(lifecycle.NeverStarted); !never {
		t.Fatalf("a run killed before its start is %#v, want dead and never started", dead)
	}
	// A daemon that tells a supervisor proved dead to start learns it never started.
	var gone supervisor.Gone
	if err := h.Start(ctx); !errors.As(err, &gone) || gone.Start != (lifecycle.NeverStarted{}) {
		t.Fatalf("Start of a supervisor proved dead = %v, want it gone and never started", err)
	}
	run, err := r.Restart().Probe(ctx, l.Ref())
	if err != nil {
		t.Fatalf("Probe after a restart: %v", err)
	}
	if d, ok := run.(supervisor.Dead); !ok || d.Start != (lifecycle.NeverStarted{}) {
		t.Fatalf("after a restart the run is %#v, want dead and never started", run)
	}
}

// awaitDead returns the run of a job whose supervisor was killed and whose connection ended. Its
// lock can outlive the process for a moment, so proof can take another look: each look yields to
// the rest of the program first, and only ctx, a guard against a hang, bounds them.
func awaitDead(t *testing.T, ctx context.Context, sups supervisor.Supervisors, ref lifecycle.Ref) supervisor.Dead {
	t.Helper()
	for {
		run, err := sups.Probe(ctx, ref)
		if err != nil {
			t.Fatalf("Probe: %v", err)
		}
		if d, ok := run.(supervisor.Dead); ok {
			return d
		}
		if ctx.Err() != nil {
			t.Fatalf("the killed supervisor never proved dead: %#v", run)
		}
		runtime.Gosched()
	}
}

// A run's output is numbered from its launch's output base, the length of the job's log before the
// run, so a daemon follows each attempt from the log's own offset.
func testOutputBase(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "alpha")
	l.OutputBase = 100
	h := start(t, r, l)
	got, _, err := follow(ctx, h, 100)
	if err != nil || string(got) != "alpha\n" {
		t.Fatalf("from the base: output %q, error %v", got, err)
	}
	if got, _, err := follow(ctx, h, 102); err != nil || string(got) != "pha\n" {
		t.Fatalf("from inside the output: output %q, error %v", got, err)
	}
	var f *core.Failure
	if _, _, err := follow(ctx, h, 99); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Fatalf("from before the base: error %v, want invalid input", err)
	}
}

func testRestartRunning(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "before", ";", "hold", "g", ";", "print", "after")
	h := start(t, r, l)
	w := watch(ctx, h, 0)
	w.until(t, ctx, "before")
	held := uint64(len(w.output()))

	// The daemon goes away and a new one comes up.
	sups := r.Restart()
	run, ok := find(t, sups, l.Ref()).(supervisor.Alive)
	if !ok || run.Handle == nil || run.Phase != lifecycle.RunRunning {
		t.Fatalf("after a restart the run is %+v, want alive and running", run)
	}
	defer run.Handle.Close()
	r.Open("g")
	got, o, err := follow(ctx, run.Handle, held)
	if err != nil {
		t.Fatalf("Follow after a restart: %v", err)
	}
	if string(got) != "after\n" || execOf(o).Kind != core.ExecExited {
		t.Errorf("resumed output %q, outcome %+v; want the rest, once", got, o)
	}
}

func testRestartFinished(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "done", ";", "exit", "7")
	h := start(t, r, l)
	if _, _, err := follow(ctx, h, 0); err != nil {
		t.Fatal(err)
	}
	// The command ended while no daemon looked; the supervisor keeps the outcome.
	run, ok := find(t, r.Restart(), l.Ref()).(supervisor.Alive)
	if !ok || run.Phase != lifecycle.RunFinished {
		t.Fatalf("an ended job's run is %+v, want alive and finished", run)
	}
	defer run.Handle.Close()
	got, o, err := follow(ctx, run.Handle, 0)
	if err != nil || string(got) != "done\n" || execOf(o) != (core.ExecOutcome{Kind: core.ExecExited, ExitCode: 7}) {
		t.Fatalf("output %q, outcome %+v, error %v", got, o, err)
	}
}

func testRuns(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	var refs []lifecycle.Ref
	for i := 0; i < 3; i++ {
		l := launch(fmt.Sprintf("j%d", i), "print", "x")
		if _, err := r.Supervisors.Spawn(ctx, l); err != nil {
			t.Fatal(err)
		}
		refs = append(refs, l.Ref())
	}
	got := map[lifecycle.Ref]supervisor.Alive{}
	for _, run := range all(t, r.Supervisors) {
		alive, ok := run.(supervisor.Alive)
		if !ok || alive.Handle == nil || alive.Phase != lifecycle.RunParked {
			t.Errorf("run %s = %+v, want alive and parked", run.Job(), run)
			continue
		}
		defer alive.Handle.Close()
		got[run.Job()] = alive
	}
	for _, ref := range refs {
		if _, ok := got[ref]; !ok {
			t.Errorf("run %s is not listed", ref)
		}
	}
	var f *core.Failure
	if err := r.Supervisors.Forget(ctx, refs[0], lifecycle.ProofLockFree); !errors.As(err, &f) || f.Kind != core.FailureConflict {
		t.Errorf("Forget of a live run = %v, want a conflict", err)
	}
	if err := got[refs[0]].Handle.Release(ctx); err != nil {
		t.Fatal(err)
	}
	if err := r.Supervisors.Forget(ctx, refs[0], lifecycle.ProofReleased); err != nil {
		t.Errorf("Forget after Release: %v", err)
	}
	if err := r.Supervisors.Forget(ctx, refs[0], lifecycle.ProofReleased); err != nil {
		t.Errorf("Forget twice: %v", err)
	}
}

// Closing a handle lets go of the connection and of nothing else.
func testCloseLeavesTheSupervisor(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "up", ";", "hold", "g", ";", "print", "after")
	h := start(t, r, l)
	h.Close()
	h.Close()
	r.Open("g")
	run, ok := find(t, r.Supervisors, l.Ref()).(supervisor.Alive)
	if !ok {
		t.Fatalf("after Close the run is %+v, want alive", run)
	}
	defer run.Handle.Close()
	got, o, err := follow(ctx, run.Handle, 0)
	if err != nil || string(got) != "up\nafter\n" || execOf(o).Kind != core.ExecExited {
		t.Fatalf("output %q, outcome %+v, error %v", got, o, err)
	}
}

func testSpawnOnce(t *testing.T, r Rig) {
	ctx := ctxFor(t)
	l := launch("j1", "print", "x")
	if _, err := r.Supervisors.Spawn(ctx, l); err != nil {
		t.Fatal(err)
	}
	var f *core.Failure
	if _, err := r.Supervisors.Spawn(ctx, l); !errors.As(err, &f) || f.Kind != core.FailureConflict {
		t.Fatalf("second Spawn = %v, want a conflict", err)
	}
}

func all(t *testing.T, s supervisor.Supervisors) []supervisor.Run {
	t.Helper()
	runs, err := s.Runs(ctxFor(t))
	if err != nil {
		t.Fatalf("Runs: %v", err)
	}
	return runs
}

func find(t *testing.T, s supervisor.Supervisors, ref lifecycle.Ref) supervisor.Run {
	t.Helper()
	for _, run := range all(t, s) {
		if run.Job() == ref {
			return run
		}
	}
	t.Fatalf("no run for %s", ref)
	return nil
}

// execOf is how the command of an outcome ended, zero when no ending was seen.
func execOf(o lifecycle.Outcome) core.ExecOutcome {
	switch end := o.End.(type) {
	case lifecycle.CommandEnded:
		if e, ok := end.Attempt.(core.Ended); ok {
			return e.Exec
		}
	case lifecycle.FlowEnded:
		if s, ok := end.Verdict.(lifecycle.Stopped); ok {
			return s.Exec
		}
	}
	return core.ExecOutcome{}
}

// notAttemptedOf is why an outcome's command never started, nil when it did.
func notAttemptedOf(o lifecycle.Outcome) *core.Failure {
	switch end := o.End.(type) {
	case lifecycle.CommandEnded:
		if e, ok := end.Attempt.(core.NotStarted); ok {
			return &e.Failure
		}
	case lifecycle.FlowEnded:
		if n, ok := end.Verdict.(lifecycle.NotAttempted); ok {
			return &n.Failure
		}
	}
	return nil
}
