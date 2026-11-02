package flowrun_test

import (
	"context"
	"errors"
	"fmt"
	"io"
	"math/rand/v2"
	"strings"
	"sync"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/backend/cpu"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/flowrun"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/reading"
)

// fakeRig is the shell on the fake environment, the fake checkouts and the memory file system.
type fakeRig struct {
	clock     *clock.Manual
	files     *hostfs.Memory
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	system    *cpu.FakeSystem
	backend   *cpu.Backend
}

func newFakeRig(t *testing.T) rig {
	t.Helper()
	files := hostfs.NewMemory()
	clk := clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond)
	system := cpu.NewFakeSystem(facts)
	return &fakeRig{
		clock: clk, files: files, system: system, backend: cpu.New(system, clk),
		env:       fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates(), fakeenv.WithFiles(files)),
		checkouts: &fakeenv.Checkouts{},
	}
}

func (*fakeRig) real() bool { return false }

func (r *fakeRig) config(out io.Writer) flowrun.Config {
	return flowrun.Config{
		Repository: "/repo", Commit: commit, Environment: r.env, Checkouts: r.checkouts,
		Backend: r.backend, Files: r.files, Clock: r.clock, Output: out,
	}
}

func (r *fakeRig) leaks() []string {
	left := r.files.Scratch()
	if n := r.checkouts.Open(); n != 0 {
		left = append(left, fmt.Sprintf("%d checkouts", n))
	}
	if n := r.env.Active(); n != 0 {
		left = append(left, fmt.Sprintf("%d commands", n))
	}
	return left
}

func TestScenariosOnTheFakes(t *testing.T)     { runScenarios(t, newFakeRig) }
func TestCancellationsOnTheFakes(t *testing.T) { runCancellations(t, newFakeRig) }
func TestOutputOnTheFakes(t *testing.T)        { runOutput(t, newFakeRig) }

func TestRunsEachSampleWithItsOwnFileInTheScratchDirectory(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	s := scenarios()[2]
	if _, err := flowrun.Run(context.Background(), r.config(io.Discard), s.plan(false)); err != nil {
		t.Fatal(err)
	}
	var paths []string
	for _, x := range r.env.Runs() {
		// An environment that does not share the host's files makes this directory visible to the
		// command, so every command is told which it is.
		if x.Scratch != "/memory/scratch-1" {
			t.Errorf("a command was given the scratch directory %q, want the one its result files are in", x.Scratch)
		}
		if n := len(x.Argv); n > 2 && x.Argv[n-2] == "--out" {
			paths = append(paths, x.Argv[n-1])
		}
		if x.Checkout.Dir == "" || !strings.HasPrefix(x.Checkout.Dir, "/fake-checkouts/") {
			t.Errorf("a command ran in %q, not in the checkout", x.Checkout.Dir)
		}
	}
	want := []string{"/memory/scratch-1/sample-0.out", "/memory/scratch-1/sample-1.out", "/memory/scratch-1/sample-2.out"}
	if strings.Join(paths, ",") != strings.Join(want, ",") {
		t.Errorf("result files = %v, want %v", paths, want)
	}
}

func TestACancelledContextRunsNothingAndMakesNothing(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	v, err := flowrun.Run(ctx, r.config(io.Discard), scenarios()[2].plan(false))
	if err != nil || !v.Cancelled() {
		t.Fatalf("verdict = %+v, %v", v, err)
	}
	if r.checkouts.Made() != 0 || len(r.env.Runs()) != 0 {
		t.Errorf("a cancelled job made %d checkouts and ran %d commands", r.checkouts.Made(), len(r.env.Runs()))
	}
}

func TestACheckoutThatFailsEndsTheJobBeforeAnyCommand(t *testing.T) {
	for name, err := range map[string]error{
		"a typed failure": &core.Failure{Kind: core.FailureNotFound, Message: "no such commit"},
		"a plain error":   errors.New("disk full"),
	} {
		t.Run(name, func(t *testing.T) {
			r := newFakeRig(t).(*fakeRig)
			r.checkouts.Fail = err
			v, runErr := flowrun.Run(context.Background(), r.config(io.Discard), scenarios()[0].plan(false))
			if runErr != nil {
				t.Fatal(runErr)
			}
			if v.Passed() || v.NotAttempted == nil || v.Stage != flow.StageCheckout || len(r.env.Runs()) != 0 {
				t.Errorf("verdict = %+v after running %d commands", v, len(r.env.Runs()))
			}
			var f *core.Failure
			if errors.As(err, &f) && *v.NotAttempted != *f {
				t.Errorf("failure = %+v, want the checkout's own %+v", *v.NotAttempted, *f)
			}
			if !errors.As(err, &f) && v.NotAttempted.Kind != core.FailureInternal {
				t.Errorf("a plain error became %+v, want an internal failure", *v.NotAttempted)
			}
		})
	}
}

func TestAnEnvironmentThatCannotRunIsNotAttempted(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	s := scenarios()[0]
	s.check = prog{fake: []string{"fail", "docker", "is", "down"}}
	v, err := flowrun.Run(context.Background(), r.config(io.Discard), s.plan(false))
	if err != nil {
		t.Fatal(err)
	}
	if v.NotAttempted == nil || v.NotAttempted.Kind != core.FailureUnavailable || v.Stage != flow.StageCheck {
		t.Errorf("verdict = %+v", v)
	}
	if left := r.leaks(); len(left) > 0 {
		t.Errorf("left %v", left)
	}
}

func TestAMachineThatCannotBeReadDoesNotStopTheJob(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	r.system.FailWith(errors.New("no /proc"))
	v, err := flowrun.Run(context.Background(), r.config(io.Discard), scenarios()[2].plan(false))
	if err != nil || !v.Passed() {
		t.Fatalf("verdict = %+v, %v", v, err)
	}
	if v.Findings.Machine != (core.MachineFacts{}) {
		t.Errorf("machine = %+v, want it empty", v.Findings.Machine)
	}
}

func TestAnInvalidPlanIsRefusedBeforeAnythingIsMade(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	p := scenarios()[2].plan(false)
	p.Samples = 0
	if _, err := flowrun.Run(context.Background(), r.config(io.Discard), p); err == nil {
		t.Fatal("a plan with no samples was followed")
	}
	if r.checkouts.Made() != 0 {
		t.Error("an invalid plan made a checkout")
	}
}

// What the shell reads is exactly what the command wrote, whatever the bytes, and the limit is the
// reader's.
func TestTheResultFileIsReadWholeAndWithinTheLimit(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	sh := flowrun.New(r.config(io.Discard))
	defer sh.Close()
	ctx := context.Background()
	co, ok := sh.Do(ctx, flow.Checkout{}).(flow.CheckedOut)
	if !ok {
		t.Fatal("no checkout")
	}
	read := func(path string) flow.Ran {
		return sh.Do(ctx, flow.Run{Stage: flow.StageBenchmark, Argv: []string{"exit", "0"}, Timeout: time.Minute, Read: path}).(flow.Ran)
	}
	rng := rand.New(rand.NewPCG(7, 11))
	for i := 0; i < 300; i++ {
		data := make([]byte, rng.IntN(70000))
		for j := range data {
			data[j] = byte(rng.IntN(256))
		}
		path := fmt.Sprintf("%s/f%d", co.Scratch, i)
		r.files.Put(path, data)
		got := read(path)
		if !got.Found || got.ReadError != "" || string(got.Output) != string(data) {
			t.Fatalf("file %d of %d bytes: found=%v err=%q, %d bytes back", i, len(data), got.Found, got.ReadError, len(got.Output))
		}
	}
	if got := read(co.Scratch + "/nothing"); got.Found || got.ReadError != "" || got.Output != nil {
		t.Errorf("a missing file: %+v", got)
	}
	r.files.Put(co.Scratch+"/limit", make([]byte, reading.MaxFileBytes))
	if got := read(co.Scratch + "/limit"); !got.Found || got.ReadError != "" || len(got.Output) != reading.MaxFileBytes {
		t.Errorf("a file at the limit: found=%v err=%q", got.Found, got.ReadError)
	}
	r.files.Put(co.Scratch+"/over", make([]byte, reading.MaxFileBytes+1))
	if got := read(co.Scratch + "/over"); !got.Found || got.ReadError == "" || got.Output != nil {
		t.Errorf("a file over the limit: found=%v err=%q", got.Found, got.ReadError)
	}
}

// A file the command wrote is read whatever the command's ending, and the machine decides what to do
// with it: the shell never drops a file because the command failed.
func TestTheFileIsReadAfterACommandThatFailed(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	sh := flowrun.New(r.config(io.Discard))
	defer sh.Close()
	ctx := context.Background()
	co := sh.Do(ctx, flow.Checkout{}).(flow.CheckedOut)
	path := co.Scratch + "/x.out"
	got := sh.Do(ctx, flow.Run{Stage: flow.StageBenchmark, Argv: []string{"emit", "partial", ";", "exit", "5", "--out", path}, Timeout: time.Minute, Read: path}).(flow.Ran)
	if got.Exec.ExitCode != 5 || !got.Found || string(got.Output) != "partial" {
		t.Errorf("Ran = %+v", got)
	}
	if !got.Ended.After(got.Started) {
		t.Errorf("the command ended at %v, not after it started at %v", got.Ended, got.Started)
	}
}

// Closing removes the scratch directory and the checkout, and does so once.
func TestCloseRemovesWhatTheShellMade(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	sh := flowrun.New(r.config(io.Discard))
	if _, ok := sh.Do(context.Background(), flow.Checkout{}).(flow.CheckedOut); !ok {
		t.Fatal("no checkout")
	}
	if len(r.leaks()) == 0 {
		t.Fatal("nothing was made")
	}
	var wg sync.WaitGroup
	for range 4 {
		wg.Go(sh.Close)
	}
	wg.Wait()
	if left := r.leaks(); len(left) > 0 {
		t.Errorf("left %v", left)
	}
}

// cancelAsError is an environment that reports a cancel as the context's error and not as an
// outcome, as an environment does that learns of it only from the call it was making.
type cancelAsError struct{ environment.Environment }

func (e cancelAsError) Run(ctx context.Context, x core.Execution, out io.Writer) (core.ExecOutcome, error) {
	got, err := e.Environment.Run(ctx, x, out)
	if err == nil && got.Kind == core.ExecCancelled {
		return core.ExecOutcome{}, ctx.Err()
	}
	return got, err
}

func TestACancelThatSurfacesAsAnErrorIsStillACancel(t *testing.T) {
	r := newFakeRig(t).(*fakeRig)
	s := stageHangs()[2]
	cfg := r.config(nil)
	out := newLineWatch()
	cfg.Output = out
	cfg.Environment = cancelAsError{r.env}
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	var (
		wg      sync.WaitGroup
		verdict flow.Verdict
	)
	wg.Go(func() { verdict, _ = flowrun.Run(ctx, cfg, s.plan(false)) })
	awaitMarker(t, out)
	cancel()
	wg.Wait()
	if !verdict.Cancelled() || verdict.Stage != flow.StageBenchmark || verdict.NotAttempted != nil {
		t.Errorf("verdict = %+v", verdict)
	}
}
