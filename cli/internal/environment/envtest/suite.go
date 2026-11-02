// Package envtest is the contract suite every environment.Environment passes, written so that the
// host environment, the docker environment and the fake the daemon's simulation uses are all held
// to the same behavior.
//
// The suite does not run shell commands. It describes what a command does as a Program and asks a
// World to render it, so an implementation that runs shell commands, one that runs containers and
// a fake that interprets scripts each receive a command they understand. Wait points, signals and
// "is this process still running" go through the World too, and none polls: a wait ends when the
// process it waits for produces the event. Every timeout and pause an implementation takes goes
// through the clock.Timers it is given, which the suite fires itself.
//
// What the suite requires of Run is stated by the cases, which read as the contract:
//
//   - the timer an execution is limited by is the first it arms, with the duration of its Timeout;
//     a pause between two readiness probes is a timer armed after the first
//   - the whole process group of the command and of the service ends on every path: an exit, the
//     limit, a cancellation, a service that never became ready, and the death of the process that
//     called Run
package envtest

import (
	"bytes"
	"context"
	"errors"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Backstop bounds every wait in the suite. It only stops a hung implementation from hanging the
// build: no case passes or fails by how long it took.
const Backstop = time.Minute

// limit is the Timeout the suite gives its executions. It never elapses: the suite fires the
// limit's timer when it wants the limit to be reached.
const limit = time.Hour

// Rig is what the suite needs to test one implementation.
type Rig struct {
	// New returns the environment under test, which runs its commands in the world. Every
	// timeout and pause it takes must come from timers. It is called once per case, after World.
	New func(t *testing.T, w World, timers clock.Timers) environment.Environment

	// World returns the world the programs run in, fresh for each case. A ShellWorld serves any
	// implementation that runs shell commands on this machine's kernel.
	World func(t *testing.T) World

	// Checkout returns a directory to run in, fresh for each case, removed when the case ends.
	// It gets the case's world, since an environment whose commands run elsewhere, such as in a
	// container, can reach the world only through the directory it mounts.
	Checkout func(t *testing.T, w World) core.Checkout

	// Spawn runs Run(x) in a separate process that is not the test's own, with the same
	// environment New returns and the same world, and returns the function that kills that
	// process abruptly (SIGKILL) and returns once it is gone. An implementation with no process
	// to kill, such as a fake, ends the run in the way that is nearest to it.
	Spawn func(t *testing.T, w World, x core.Execution) (kill func())
}

// Run runs the suite.
func Run(t *testing.T, rig Rig) {
	t.Helper()
	cases := []struct {
		name string
		fn   func(h *harness)
	}{
		{"a command that exits reports its exit code", caseExitCodes},
		{"a command killed by a signal reports 128 plus the signal", caseSignaled},
		{"a command that does nothing exits with code 0", caseEmpty},
		{"stdout and stderr are one stream in the order they were written", caseOutputOrder},
		{"the command runs in the checkout", caseWorkingDirectory},
		{"output is delivered while the command still runs", caseStreaming},
		{"a command that cannot be started is never a success", caseMissingCommand},
		{"a cancelled context before the start runs nothing", caseCancelledBeforeStart},
		{"an execution that cannot be valid is refused and runs nothing", caseInvalid},
		{"the fingerprint is stable and pins the environment", caseFingerprint},
		{"cancelling ends the run and its whole process group", caseCancel},
		{"the limit ends the run and its whole process group", caseLimit},
		{"a normal exit ends the processes the command left behind", caseLeftovers},
		{"the death of the caller ends the process group", caseCallerDies},
		{"a service without a probe is ready at once and ends with the run", caseServiceNoProbe},
		{"the command waits for the service's probe, pausing on the timers", caseServiceProbe},
		{"a service that is never ready ends the run at the limit", caseServiceNeverReady},
		{"a service that exits before it is ready ends the run", caseServiceExitsEarly},
		{"a service that exits while the command runs ends the run", caseServiceExitsLate},
		{"cancelling while the service is not ready ends both groups", caseServiceCancelWaiting},
		{"the limit during the command ends both groups", caseServiceLimit},
		{"the death of the caller ends the service's group too", caseServiceCallerDies},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			h := newHarness(t, rig)
			c.fn(h)
		})
	}
}

// harness is one case's environment, world and checkout, and the runs it started.
type harness struct {
	t        *testing.T
	rig      Rig
	env      environment.Environment
	timers   *Timers
	world    World
	checkout core.Checkout

	ctx    context.Context
	cancel context.CancelFunc
	runs   sync.WaitGroup
}

func newHarness(t *testing.T, rig Rig) *harness {
	t.Helper()
	h := &harness{t: t, rig: rig, timers: NewTimers()}
	h.ctx, h.cancel = context.WithTimeout(context.Background(), Backstop)
	h.world = rig.World(t)
	h.checkout = rig.Checkout(t, h.world)
	h.env = rig.New(t, h.world, h.timers)
	t.Cleanup(func() {
		// A case that failed may leave a run waiting: end it so the cleanup does not hang.
		h.cancel()
		h.runs.Wait()
	})
	return h
}

// exec makes an execution of a program in the checkout.
func (h *harness) exec(p Program) core.Execution {
	return core.Execution{Checkout: h.checkout, Argv: h.world.Argv(p), Timeout: limit}
}

// capture is an output writer the test can wait on.
type capture struct {
	mu      sync.Mutex
	buf     bytes.Buffer
	changed chan struct{}
}

func newCapture() *capture { return &capture{changed: make(chan struct{})} }

func (c *capture) Write(p []byte) (int, error) {
	c.mu.Lock()
	c.buf.Write(p)
	changed := c.changed
	c.changed = make(chan struct{})
	c.mu.Unlock()
	close(changed)
	return len(p), nil
}

func (c *capture) String() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	return c.buf.String()
}

// await waits until the output contains the text.
func (c *capture) await(ctx context.Context, text string) bool {
	for {
		c.mu.Lock()
		has := strings.Contains(c.buf.String(), text)
		changed := c.changed
		c.mu.Unlock()
		if has {
			return true
		}
		select {
		case <-changed:
		case <-ctx.Done():
			return false
		}
	}
}

// running is a Run in progress.
type running struct {
	h      *harness
	out    *capture
	cancel context.CancelFunc
	done   chan struct{}

	outcome core.ExecOutcome
	err     error
}

// start begins a Run in the background. Its context is cancelled by cancel().
func (h *harness) start(x core.Execution) *running {
	h.t.Helper()
	ctx, cancel := context.WithCancel(h.ctx)
	r := &running{h: h, out: newCapture(), cancel: cancel, done: make(chan struct{})}
	h.runs.Go(func() {
		defer close(r.done)
		r.outcome, r.err = h.env.Run(ctx, x, r.out)
	})
	return r
}

// wait waits for the Run to return.
func (r *running) wait() (core.ExecOutcome, error) {
	r.h.t.Helper()
	select {
	case <-r.done:
		return r.outcome, r.err
	case <-r.h.ctx.Done():
		r.h.t.Fatalf("Run did not return: the run is stuck")
		return core.ExecOutcome{}, nil
	}
}

// run runs the program to its end.
func (h *harness) run(p Program) (core.ExecOutcome, string) {
	h.t.Helper()
	r := h.start(h.exec(p))
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	return out, r.out.String()
}

func (h *harness) reached(point string) {
	h.t.Helper()
	if !h.world.Reached(h.ctx, point) {
		h.t.Fatalf("the program never reached %q", point)
	}
}

// gone requires that the processes that ran Alive(name) have ended.
func (h *harness) gone(names ...string) {
	h.t.Helper()
	for _, name := range names {
		if !h.world.Gone(h.ctx, name) {
			h.t.Fatalf("the process %q still runs after the run ended", name)
		}
	}
}

func (h *harness) timer(n int) *Timer {
	h.t.Helper()
	timer := h.timers.Await(h.ctx, n)
	if timer == nil {
		h.t.Fatalf("the environment never armed timer %d", n)
	}
	return timer
}

// limitTimer returns the timer the execution is limited by.
func (h *harness) limitTimer() *Timer {
	h.t.Helper()
	timer := h.timer(1)
	if timer.Duration != limit {
		h.t.Fatalf("the first timer is for %v, want the execution's Timeout %v", timer.Duration, limit)
	}
	return timer
}

func expect(t *testing.T, got core.ExecOutcome, kind core.ExecKind) {
	t.Helper()
	if got.Kind != kind {
		t.Fatalf("outcome %+v, want %s", got, kind)
	}
}

func expectExit(t *testing.T, got core.ExecOutcome, code int) {
	t.Helper()
	if got.Kind != core.ExecExited || got.ExitCode != code {
		t.Fatalf("outcome %+v, want exited with code %d", got, code)
	}
}

// tree is a program with a process of its own in the background and a main process that waits
// for ever: the group the cases check ends. The processes are observable as "main" and "child".
func tree(extra ...Step) Program {
	p := Program{Alive("main"), Child(Alive("child"), Hold(Forever)), Mark("up")}
	p = append(p, extra...)
	return append(p, Hold(Forever))
}

func caseExitCodes(h *harness) {
	for _, code := range []int{0, 1, 2, 7, 126, 127, 255} {
		out, _ := h.run(Program{Exit(code)})
		expectExit(h.t, out, code)
	}
}

func caseSignaled(h *harness) {
	out, _ := h.run(Program{Crash()})
	expectExit(h.t, out, 137)
}

func caseEmpty(h *harness) {
	out, text := h.run(Program{})
	expectExit(h.t, out, 0)
	if text != "" {
		h.t.Errorf("output %q, want none", text)
	}
}

func caseOutputOrder(h *harness) {
	out, text := h.run(Program{Print("one"), Warn("two"), Print("three"), Warn("four")})
	expectExit(h.t, out, 0)
	if want := "one\ntwo\nthree\nfour\n"; text != want {
		h.t.Errorf("output %q, want %q", text, want)
	}
}

func caseWorkingDirectory(h *harness) {
	out, text := h.run(Program{Dir()})
	expectExit(h.t, out, 0)
	got, want := strings.TrimSpace(text), canonical(h.checkout.Dir)
	if canonical(got) != want {
		h.t.Errorf("working directory %q, want the checkout %q", got, want)
	}
}

// canonical resolves symbolic links when the directory exists, as the command sees the path.
func canonical(dir string) string {
	if real, err := filepath.EvalSymlinks(dir); err == nil {
		return real
	}
	return dir
}

func caseStreaming(h *harness) {
	r := h.start(h.exec(Program{Print("first"), Mark("printed"), Hold("go"), Print("second")}))
	h.reached("printed")
	if !r.out.await(h.ctx, "first\n") {
		h.t.Fatalf("the output did not show %q while the command waited; it was %q", "first", r.out.String())
	}
	if strings.Contains(r.out.String(), "second") {
		h.t.Fatalf("the output shows what the command has not written yet: %q", r.out.String())
	}
	h.world.Release("go")
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expectExit(h.t, out, 0)
	if want := "first\nsecond\n"; r.out.String() != want {
		h.t.Errorf("output %q, want %q", r.out.String(), want)
	}
}

func caseMissingCommand(h *harness) {
	x := core.Execution{Checkout: h.checkout, Argv: h.world.Missing(), Timeout: limit}
	out, err := h.env.Run(h.ctx, x, newCapture())
	if err == nil && out.Kind == core.ExecExited && out.ExitCode == 0 {
		h.t.Fatalf("a command that does not exist reported success")
	}
}

func caseCancelledBeforeStart(h *harness) {
	ctx, cancel := context.WithCancel(h.ctx)
	cancel()
	out, err := h.env.Run(ctx, h.exec(Program{Mark("ran")}), newCapture())
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecCancelled)
	if h.world.Marked("ran") {
		h.t.Errorf("the command ran although the context was cancelled before the run")
	}
}

func caseInvalid(h *harness) {
	ok := h.exec(Program{Mark("ran")})
	service := &core.ServiceSpec{Argv: h.world.Argv(Program{Hold(Forever)})}
	cases := map[string]func(x *core.Execution){
		"no command":             func(x *core.Execution) { x.Argv = nil },
		"an empty program name":  func(x *core.Execution) { x.Argv = []string{""} },
		"no timeout":             func(x *core.Execution) { x.Timeout = 0 },
		"a negative timeout":     func(x *core.Execution) { x.Timeout = -time.Second },
		"no checkout directory":  func(x *core.Execution) { x.Checkout.Dir = "" },
		"a relative checkout":    func(x *core.Execution) { x.Checkout.Dir = "checkout" },
		"a service with no argv": func(x *core.Execution) { x.Service = &core.ServiceSpec{} },
		"a service with an empty program name": func(x *core.Execution) {
			x.Service = &core.ServiceSpec{Argv: []string{""}}
		},
		"a probe with an empty program name": func(x *core.Execution) {
			x.Service = &core.ServiceSpec{Argv: service.Argv, Probe: []string{""}}
		},
	}
	for name, mutate := range cases {
		x := ok
		mutate(&x)
		_, err := h.env.Run(h.ctx, x, newCapture())
		var failure *core.Failure
		if !errors.As(err, &failure) || failure.Kind != core.FailureInvalidInput {
			h.t.Errorf("%s: error %v, want an invalid_input failure", name, err)
		}
	}
	if h.world.Marked("ran") {
		h.t.Errorf("a command ran for an execution that is not valid")
	}
}

func caseFingerprint(h *harness) {
	first, err := h.env.Fingerprint(h.ctx)
	if err != nil {
		h.t.Fatalf("Fingerprint: %v", err)
	}
	second, err := h.env.Fingerprint(h.ctx)
	if err != nil || !first.Equal(second) {
		h.t.Errorf("Fingerprint changed between calls: %v then %v (%v)", first, second, err)
	}
	if first.Kind != h.env.Kind() {
		h.t.Errorf("Fingerprint kind %q, but the environment is %q", first.Kind, h.env.Kind())
	}
	_, err = core.NewJobKey(core.KeyInputs{
		Kind: core.JobCheck, Commit: core.Commit(strings.Repeat("a", 40)), TaskVersion: "task-version",
		Environment: first, Settings: core.CheckSettings{}, SchemaVersion: 1,
	})
	if err != nil {
		h.t.Errorf("the fingerprint %v cannot be part of a job key: %v", first, err)
	}
}

func caseCancel(h *harness) {
	r := h.start(h.exec(tree()))
	h.reached("up")
	h.reached(AliveMark("child"))
	r.cancel()
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecCancelled)
	h.gone("main", "child")
}

func caseLimit(h *harness) {
	r := h.start(h.exec(tree()))
	h.reached("up")
	h.reached(AliveMark("child"))
	h.limitTimer().Fire()
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecTimedOut)
	h.gone("main", "child")
}

func caseLeftovers(h *harness) {
	r := h.start(h.exec(Program{Child(Alive("child"), Hold(Forever)), Hold("go"), Exit(3)}))
	h.reached(AliveMark("child"))
	h.world.Release("go")
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expectExit(h.t, out, 3)
	h.gone("child")
}

// caseCallerDies starts the run from a process the suite kills, and requires that what the run
// started does not outlive it: the next job must never start beside it.
func caseCallerDies(h *harness) {
	if h.rig.Spawn == nil {
		h.t.Fatal("the rig has no Spawn: an implementation must show that the death of its caller ends the group")
	}
	kill := h.rig.Spawn(h.t, h.world, h.exec(tree()))
	h.reached("up")
	h.reached(AliveMark("child"))
	kill()
	h.gone("main", "child")
}

// serviceProgram is a service that is observable as "service" and whose group has a process of
// its own, observable as "service-child".
func serviceProgram(extra ...Step) Program {
	p := Program{Alive("service"), Child(Alive("service-child"), Hold(Forever)), Mark("service-up")}
	p = append(p, extra...)
	return append(p, Hold(Forever))
}

func (h *harness) withService(p Program, svc Program, probe Program) core.Execution {
	x := h.exec(p)
	x.Service = &core.ServiceSpec{Argv: h.world.Argv(svc)}
	if probe != nil {
		x.Service.Probe = h.world.Argv(probe)
	}
	return x
}

func caseServiceNoProbe(h *harness) {
	x := h.withService(Program{Mark("ran"), Hold("go"), Exit(0)}, serviceProgram(), nil)
	r := h.start(x)
	h.reached("ran")
	h.reached(AliveMark("service"))
	h.reached(AliveMark("service-child"))
	h.world.Release("go")
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expectExit(h.t, out, 0)
	h.gone("service", "service-child")
}

func caseServiceProbe(h *harness) {
	x := h.withService(Program{Mark("ran"), Exit(0)}, serviceProgram(), Program{UnlessFlag("ready")})
	r := h.start(x)
	h.limitTimer()
	// The first probe fails because the flag is not set, and the environment then waits on a
	// timer before it probes again. Timer 2 being armed says it did.
	pause := h.timer(2)
	if h.world.Marked("ran") {
		h.t.Fatalf("the command ran before the service was ready")
	}
	h.world.SetFlag("ready")
	pause.Fire()
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expectExit(h.t, out, 0)
	if !h.world.Marked("ran") {
		h.t.Errorf("the command never ran although the service became ready")
	}
	h.gone("service", "service-child")
}

func caseServiceNeverReady(h *harness) {
	x := h.withService(Program{Mark("ran"), Exit(0)}, serviceProgram(), Program{UnlessFlag("ready")})
	r := h.start(x)
	limit := h.limitTimer()
	h.timer(2)
	h.reached(AliveMark("service-child"))
	limit.Fire()
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecServiceNotReady)
	if h.world.Marked("ran") {
		h.t.Errorf("the command ran although the service was never ready")
	}
	h.gone("service", "service-child")
}

func caseServiceExitsEarly(h *harness) {
	svc := Program{Alive("service"), Child(Alive("service-child"), Hold(Forever)), Hold("die"), Exit(3)}
	x := h.withService(Program{Mark("ran"), Exit(0)}, svc, Program{UnlessFlag("ready")})
	r := h.start(x)
	h.timer(2) // the first probe failed and the environment is waiting to try again
	h.reached(AliveMark("service-child"))
	h.world.Release("die")
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecServiceExited)
	if h.world.Marked("ran") {
		h.t.Errorf("the command ran although the service exited before it was ready")
	}
	h.gone("service", "service-child")
}

func caseServiceExitsLate(h *harness) {
	svc := Program{Alive("service"), Child(Alive("service-child"), Hold(Forever)), Hold("die"), Exit(2)}
	r := h.start(h.withService(tree(), svc, nil))
	h.reached("up")
	h.reached(AliveMark("child"))
	h.reached(AliveMark("service-child"))
	h.world.Release("die")
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecServiceExited)
	h.gone("main", "child", "service", "service-child")
}

func caseServiceCancelWaiting(h *harness) {
	x := h.withService(Program{Mark("ran"), Exit(0)}, serviceProgram(), Program{UnlessFlag("ready")})
	r := h.start(x)
	h.timer(2)
	h.reached(AliveMark("service-child"))
	r.cancel()
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecCancelled)
	if h.world.Marked("ran") {
		h.t.Errorf("the command ran although the run was cancelled before the service was ready")
	}
	h.gone("service", "service-child")
}

func caseServiceLimit(h *harness) {
	r := h.start(h.withService(tree(), serviceProgram(), nil))
	h.reached("up")
	h.reached(AliveMark("child"))
	h.reached(AliveMark("service-child"))
	h.limitTimer().Fire()
	out, err := r.wait()
	if err != nil {
		h.t.Fatalf("Run: %v", err)
	}
	expect(h.t, out, core.ExecTimedOut)
	h.gone("main", "child", "service", "service-child")
}

func caseServiceCallerDies(h *harness) {
	if h.rig.Spawn == nil {
		h.t.Fatal("the rig has no Spawn: an implementation must show that the death of its caller ends the group")
	}
	kill := h.rig.Spawn(h.t, h.world, h.withService(tree(), serviceProgram(), nil))
	h.reached("up")
	h.reached(AliveMark("child"))
	h.reached(AliveMark("service-child"))
	kill()
	h.gone("main", "child", "service", "service-child")
}
