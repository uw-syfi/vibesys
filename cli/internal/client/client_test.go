package client_test

import (
	"context"
	"errors"
	"fmt"
	"net"
	"strings"
	"sync"
	"syscall"
	"testing"
	"time"

	"google.golang.org/grpc"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

const backstop = time.Minute

var (
	commit = core.Commit("0123456789abcdef0123456789abcdef01234567")
	call   = &cliv1.CallContext{Repository: "/repo", Task: "task", RunId: "run-1", Harness: "h", Agent: "agent-1"}
)

func ctxFor(t *testing.T) context.Context {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), backstop)
	t.Cleanup(cancel)
	return ctx
}

type setup struct {
	dir     runtimedir.Dir
	world   *daemontest.World
	starter *daemontest.Starter
	timers  *clock.ManualTimers
}

func newSetup(t *testing.T) *setup {
	t.Helper()
	s := &setup{dir: daemontest.RuntimeDir(t), world: daemontest.NewWorld(t), timers: clock.NewManualTimers()}
	s.world.Git.AddRepo("/repo", nil)
	s.starter = &daemontest.Starter{Dir: s.dir, Version: "test", Runtime: s.world.Runtime}
	t.Cleanup(func() { s.starter.Close(t) })
	return s
}

func (s *setup) options(start bool) client.Options {
	o := client.Options{Rendezvous: client.LocalRendezvous{Dir: s.dir}, Version: "test", Timers: s.timers}
	if start {
		o.Starter = s.starter
	}
	return o
}

func (s *setup) connect(t *testing.T) *client.Client {
	t.Helper()
	c, err := client.Connect(ctxFor(t), s.options(true))
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	t.Cleanup(func() { c.Close() })
	return c
}

func submit(t *testing.T, c *client.Client, argv ...string) *cliv1.Job {
	t.Helper()
	resp, err := c.Submit(ctxFor(t), &cliv1.SubmitRequest{Context: call, Spec: wire.JobSpec(core.JobSpec{
		Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Image: core.ImageHarness, Argv: argv}})})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	return resp.GetJob()
}

func TestDialDoesNotStartADaemon(t *testing.T) {
	s := newSetup(t)
	_, err := client.Dial(ctxFor(t), s.options(true))
	if !errors.Is(err, client.ErrNoDaemon) {
		t.Fatalf("Dial with no daemon = %v, want ErrNoDaemon", err)
	}
	if s.starter.Calls() != 0 {
		t.Fatal("Dial started a daemon")
	}
}

func TestConnectWithoutAStarterFindsNoDaemon(t *testing.T) {
	s := newSetup(t)
	if _, err := client.Connect(ctxFor(t), s.options(false)); !errors.Is(err, client.ErrNoDaemon) {
		t.Fatalf("Connect = %v, want ErrNoDaemon", err)
	}
}

func TestConnectStartsADaemonOnceAndLaterClientsUseIt(t *testing.T) {
	s := newSetup(t)
	first := s.connect(t)
	second := s.connect(t)
	if got := s.starter.Calls(); got != 1 {
		t.Fatalf("the daemon was started %d times, want 1", got)
	}
	st1, err1 := first.Status(ctxFor(t))
	st2, err2 := second.Status(ctxFor(t))
	if err1 != nil || err2 != nil || st1.GetPid() != st2.GetPid() {
		t.Fatalf("status %v %v, errors %v %v", st1, st2, err1, err2)
	}
}

// Clients that find no daemon at the same moment start one between them.
func TestConcurrentClientsStartOneDaemon(t *testing.T) {
	for range 10 {
		s := newSetup(t)
		const n = 8
		var wg sync.WaitGroup
		clients := make([]*client.Client, n)
		errs := make([]error, n)
		for i := range n {
			wg.Add(1)
			go func() {
				defer wg.Done()
				clients[i], errs[i] = client.Connect(ctxFor(t), s.options(true))
			}()
		}
		wg.Wait()
		for i := range n {
			if errs[i] != nil {
				t.Fatalf("Connect %d: %v", i, errs[i])
			}
			clients[i].Close()
		}
		if got := s.starter.Calls(); got != 1 {
			t.Fatalf("%d clients started the daemon %d times, want once", n, got)
		}
		s.starter.Close(t)
	}
}

func TestWaitAllReturnsWhenEveryJobIsFinal(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	a := submit(t, c, "print", "a", ";", "exit", "0")
	b := submit(t, c, "print", "b", ";", "exit", "3")
	res, err := c.Wait(ctxFor(t), call, []string{a.GetId(), b.GetId()}, client.WaitOptions{})
	if err != nil {
		t.Fatal(err)
	}
	o := res.Outcome
	if o.GetKind() != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_SOME_FAILED || o.GetDecidedBy() != b.GetId() {
		t.Fatalf("outcome = %v, want some failed, decided by %s", o, b.GetId())
	}
	if len(o.GetJobs()) != 2 || o.GetJobs()[0].GetId() != a.GetId() || o.GetJobs()[1].GetId() != b.GetId() {
		t.Fatalf("jobs = %v, want both in the order asked", o.GetJobs())
	}
}

func TestWaitAnyReturnsAtTheFirstFinalJob(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	// Jobs run one at a time, in order: the fast job ends while the slow one waits its turn.
	fast := submit(t, c, "exit", "0")
	slow := submit(t, c, "hold", "g", ";", "exit", "0")
	res, err := c.Wait(ctxFor(t), call, []string{slow.GetId(), fast.GetId()}, client.WaitOptions{Any: true})
	if err != nil {
		t.Fatal(err)
	}
	if o := res.Outcome; o.GetKind() != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_FIRST_SUCCEEDED || o.GetDecidedBy() != fast.GetId() {
		t.Fatalf("outcome = %v, want first succeeded, decided by %s", o, fast.GetId())
	}
	s.world.Gates.Release("g")
}

// The daemon keeps the timeout: it answers TIMED_OUT, and the job goes on and succeeds.
func TestAWaitTimeoutIsTheDaemonsAnswerAndNeverCancelsTheJob(t *testing.T) {
	s := newSetup(t)
	// The daemon's timers fire this one timeout at once, and every other wait of the daemon (no
	// other is this long) on the system's clock: no store or supervisor call can race it.
	const timeout = 45 * time.Minute
	s.starter.Timers = clock.Exactly{D: timeout, Others: clock.System{}}
	// The client's time holds still, so it asks the daemon for exactly the whole timeout.
	opts := s.options(true)
	opts.Clock = clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), 0)
	c, err := client.Connect(ctxFor(t), opts)
	if err != nil {
		t.Fatalf("Connect: %v", err)
	}
	t.Cleanup(func() { c.Close() })
	job := submit(t, c, "hold", "g", ";", "exit", "0")
	res, err := c.Wait(ctxFor(t), call, []string{job.GetId()}, client.WaitOptions{Timeout: timeout})
	if err != nil {
		t.Fatalf("Wait: %v", err)
	}
	if o := res.Outcome; o.GetKind() != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_TIMED_OUT || len(o.GetJobs()) != 1 {
		t.Fatalf("outcome = %v, want timed out with the job", o)
	}
	s.world.Gates.Release("g")
	again, err := c.Wait(ctxFor(t), call, []string{job.GetId()}, client.WaitOptions{})
	if err != nil || again.Outcome.GetKind() != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED {
		t.Fatalf("the job after a timed-out wait: %v, %v; want it to have succeeded", again.Outcome, err)
	}
}

func TestWaitOnAnUnknownJobFailsWithNotFound(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	_, err := c.Wait(ctxFor(t), call, []string{"j404"}, client.WaitOptions{})
	if f, ok := wire.FailureOf(err); !ok || f.Kind != core.FailureNotFound {
		t.Fatalf("Wait = %v, want a not_found failure", err)
	}
}

func TestLogsPassesTheOutput(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	job := submit(t, c, "print", "one", ";", "print", "two", ";", "exit", "0")
	var got string
	err := c.Logs(ctxFor(t), &cliv1.ReadLogsRequest{Context: call, JobId: job.GetId(), Follow: true}, func(r *cliv1.ReadLogsResponse) error {
		got += string(r.GetData())
		return nil
	})
	if err != nil || got != "one\ntwo\n" {
		t.Fatalf("Logs = %q, %v", got, err)
	}
}

// stubDaemon is a daemon at the socket that reports the compatibility it is told to.
type stubDaemon struct {
	cliv1.UnimplementedDaemonServiceServer
	compat  cliv1.Compatibility
	stopped chan struct{}
	once    sync.Once
}

func (d *stubDaemon) Hello(context.Context, *cliv1.HelloRequest) (*cliv1.HelloResponse, error) {
	return &cliv1.HelloResponse{Compatibility: d.compat}, nil
}

func (d *stubDaemon) Stop(context.Context, *cliv1.StopRequest) (*cliv1.StopResponse, error) {
	d.once.Do(func() { close(d.stopped) })
	return &cliv1.StopResponse{}, nil
}

// serveStub runs a stub as the daemon of dir: it holds the instance lock, and goes when stopped.
func serveStub(t *testing.T, dir runtimedir.Dir, compat cliv1.Compatibility) *stubDaemon {
	t.Helper()
	lock, err := runtimedir.TryLock(dir.DaemonLock())
	if err != nil {
		t.Fatal(err)
	}
	lis, err := net.Listen("unix", dir.Socket())
	if err != nil {
		t.Fatal(err)
	}
	stub := &stubDaemon{compat: compat, stopped: make(chan struct{})}
	gs := grpc.NewServer()
	cliv1.RegisterDaemonServiceServer(gs, stub)
	go gs.Serve(lis)
	go func() {
		<-stub.stopped
		gs.GracefulStop()
		lock.Unlock()
	}()
	t.Cleanup(func() { stub.once.Do(func() { close(stub.stopped) }) })
	return stub
}

func TestAClientOlderThanTheDaemonIsToldToUpgrade(t *testing.T) {
	s := newSetup(t)
	serveStub(t, s.dir, cliv1.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD)
	for _, connect := range []func() (*client.Client, error){
		func() (*client.Client, error) { return client.Connect(ctxFor(t), s.options(true)) },
		func() (*client.Client, error) { return client.Dial(ctxFor(t), s.options(true)) },
	} {
		_, err := connect()
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureIncompatible {
			t.Fatalf("error = %v, want an incompatible failure", err)
		}
	}
	if s.starter.Calls() != 0 {
		t.Fatal("a daemon newer than the client was replaced")
	}
}

// A daemon older than the client is stopped, and a new one takes its place.
func TestADaemonOlderThanTheClientIsReplaced(t *testing.T) {
	s := newSetup(t)
	old := serveStub(t, s.dir, cliv1.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD)
	c := s.connect(t)
	select {
	case <-old.stopped:
	default:
		t.Fatal("the old daemon was not stopped")
	}
	if got := s.starter.Calls(); got != 1 {
		t.Fatalf("the new daemon was started %d times, want 1", got)
	}
	if _, err := c.Status(ctxFor(t)); err != nil {
		t.Fatalf("the replacement does not answer: %v", err)
	}
}

// watchedRendezvous is the shipped rendezvous, and says who waited for a daemon to exit without the
// right to start one: that client can wait on a new daemon forever, since nothing ends it.
type watchedRendezvous struct {
	client.Rendezvous

	mu      sync.Mutex
	holders int
	waits   int
	unheld  int
}

func (r *watchedRendezvous) HoldStart(ctx context.Context) (func() error, error) {
	release, err := r.Rendezvous.HoldStart(ctx)
	if err != nil {
		return nil, err
	}
	r.mu.Lock()
	defer r.mu.Unlock()
	r.holders++
	return func() error {
		r.mu.Lock()
		r.holders--
		r.mu.Unlock()
		return release()
	}, nil
}

func (r *watchedRendezvous) AwaitExit(ctx context.Context) error {
	r.mu.Lock()
	r.waits++
	if r.holders == 0 {
		r.unheld++
	}
	r.mu.Unlock()
	return r.Rendezvous.AwaitExit(ctx)
}

// Replacing a daemon holds the right to start from the first look to the new daemon, so a client
// never waits for the end of a daemon another client just started.
func TestReplacingADaemonHoldsTheRightToStartThroughout(t *testing.T) {
	s := newSetup(t)
	serveStub(t, s.dir, cliv1.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD)
	watched := &watchedRendezvous{Rendezvous: client.LocalRendezvous{Dir: s.dir}}
	opts := s.options(true)
	opts.Rendezvous = watched
	c, err := client.Connect(ctxFor(t), opts)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if watched.waits == 0 {
		t.Fatal("the old daemon's exit was not waited for, so this test tests nothing")
	}
	if watched.unheld != 0 {
		t.Fatalf("%d of %d waits for a daemon's exit did not hold the right to start", watched.unheld, watched.waits)
	}
}

// noAnswer is a machine held by a daemon that does not answer and does not go: every wait for its
// end runs until it is told to stop waiting.
type noAnswer struct{ starts int }

func (*noAnswer) Dial(context.Context) (net.Conn, error) { return nil, syscall.ECONNREFUSED }

func (*noAnswer) HoldStart(context.Context) (func() error, error) {
	return func() error { return nil }, nil
}

func (*noAnswer) AwaitExit(ctx context.Context) error {
	<-ctx.Done()
	return ctx.Err()
}

func (*noAnswer) Place() client.Place { return client.Place{} }

func (n *noAnswer) Start(context.Context) error {
	n.starts++
	return runtimedir.ErrDaemonRunning
}

// A client that cannot start a daemon and cannot reach the one that is there ends in an error.
func TestADaemonThatNeitherAnswersNorGoesEndsInAnError(t *testing.T) {
	stuck := &noAnswer{}
	_, err := client.Connect(ctxFor(t), client.Options{
		Rendezvous: stuck, Starter: stuck, Version: "test", Timers: clock.Immediate{Longest: time.Hour},
	})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("Connect = %v, want an unavailable failure", err)
	}
	if stuck.starts < 2 {
		t.Fatalf("%d starts: the client gave up without looking again", stuck.starts)
	}
}

// lostRace is a start that lost to another client: its daemon exits 0 with no ready byte for the
// client, because the other client's daemon has the machine.
type lostRace struct {
	winner client.Starter
	starts int
}

func (l *lostRace) Start(ctx context.Context) error {
	l.starts++
	if l.starts == 1 {
		if err := l.winner.Start(ctx); err != nil {
			return err
		}
		return runtimedir.ErrDaemonRunning
	}
	return errors.New("the daemon that won was started over")
}

// A client that lost the race to start a daemon uses the one that won.
func TestAStartThatLostTheRaceUsesTheDaemonThatWon(t *testing.T) {
	s := newSetup(t)
	race := &lostRace{winner: s.starter}
	opts := s.options(true)
	opts.Starter = race
	opts.Timers = clock.Immediate{Longest: time.Hour}
	c, err := client.Connect(ctxFor(t), opts)
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	if _, err := c.Status(ctxFor(t)); err != nil {
		t.Fatalf("the daemon that won does not answer: %v", err)
	}
	if race.starts != 1 {
		t.Fatalf("%d starts, want 1", race.starts)
	}
}

// A restart keeps the jobs: the one that ran keeps running and ends once.
func TestRestartReplacesTheDaemonAndKeepsTheJob(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	job := submit(t, c, "print", "kept", ";", "hold", "g", ";", "exit", "0")
	// Wait until the job runs: it has output.
	if err := c.Logs(ctxFor(t), &cliv1.ReadLogsRequest{Context: call, JobId: job.GetId(), Follow: true}, func(r *cliv1.ReadLogsResponse) error {
		return errStop
	}); !errors.Is(err, errStop) {
		t.Fatal(err)
	}
	before, err := c.Status(ctxFor(t))
	if err != nil {
		t.Fatal(err)
	}
	if before.GetRunningJobs() != 1 {
		t.Fatalf("running jobs = %d, want 1", before.GetRunningJobs())
	}

	c2, err := client.Restart(ctxFor(t), s.options(true), false)
	if err != nil {
		t.Fatalf("Restart: %v", err)
	}
	defer c2.Close()
	if got := s.starter.Calls(); got != 2 {
		t.Fatalf("starts = %d, want 2", got)
	}
	s.world.Gates.Release("g")
	res, err := c2.Wait(ctxFor(t), call, []string{job.GetId()}, client.WaitOptions{})
	if err != nil || res.Outcome.GetKind() != cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED {
		t.Fatalf("the job after a restart: %v, %v; want it to have succeeded", res.Outcome, err)
	}
	var out string
	if err := c2.Logs(ctxFor(t), &cliv1.ReadLogsRequest{Context: call, JobId: job.GetId()}, func(r *cliv1.ReadLogsResponse) error {
		out += string(r.GetData())
		return nil
	}); err != nil || out != "kept\n" {
		t.Fatalf("output after a restart = %q, %v; want each line once", out, err)
	}
}

var errStop = errors.New("stop")

func TestStopEndsTheDaemonAndTheNextConnectStartsAnother(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	if err := c.Stop(ctxFor(t)); err != nil {
		t.Fatalf("Stop: %v", err)
	}
	if _, err := client.Dial(ctxFor(t), s.options(false)); !errors.Is(err, client.ErrNoDaemon) {
		t.Fatalf("Dial after Stop = %v, want ErrNoDaemon", err)
	}
	s.connect(t)
	if got := s.starter.Calls(); got != 2 {
		t.Fatalf("starts = %d, want 2", got)
	}
}

// checkRequest submits a check job of the commit, which the daemon keys by the environment's
// fingerprint before it answers.
func checkRequest(s *setup, noCache bool) *cliv1.SubmitRequest {
	s.world.Tasks.Put("/repo", commit, tasktest.Files("task"))
	return &cliv1.SubmitRequest{Context: call, NoCache: noCache, Spec: wire.JobSpec(core.JobSpec{
		Commit: commit, Environment: core.EnvironmentHost, Settings: core.CheckSettings{}})}
}

// submitHeld submits while the environment holds its fingerprint, as a docker task's image
// build does, and restarts the daemon while the submission waits for it.
func submitHeld(t *testing.T, s *setup, c *client.Client, req *cliv1.SubmitRequest) (*cliv1.SubmitResponse, error) {
	t.Helper()
	s.world.Env.HoldFingerprint("image")
	type answer struct {
		resp *cliv1.SubmitResponse
		err  error
	}
	done := make(chan answer, 1)
	go func() {
		resp, err := c.Submit(ctxFor(t), req)
		done <- answer{resp, err}
	}()
	if !s.world.Gates.Reached(ctxFor(t), fakeenv.FingerprintMark) {
		t.Fatal("the submission never reached the fingerprint")
	}
	c2, err := client.Restart(ctxFor(t), s.options(true), true)
	if err != nil {
		t.Fatalf("Restart: %v", err)
	}
	t.Cleanup(func() { c2.Close() })
	s.world.Gates.Release("image")
	// Time passes for the client only once the restart is over, so its attempts reach the next
	// daemon and never the one that is stopping. A client that sent again at once could reach
	// the stopping daemon, which cancels the call again, as many times as it has attempts: it
	// must pause first, and its pause is the one timer it arms shorter than a second.
	paused := false
	for {
		select {
		case a := <-done:
			if a.err == nil && !paused {
				t.Error("the submission was sent again with no pause after the daemon stopped")
			}
			return a.resp, a.err
		case d := <-s.timers.Armed():
			paused = paused || d < time.Second
			s.timers.Fire()
		}
	}
}

// F21: a daemon restart while a submission waits for the environment (an image build) used to
// fail the submission with "context canceled" and record nothing. The client now sends it again
// to the next daemon, which records exactly one job.
func TestASubmissionOutlivesARestartOfTheDaemon(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	resp, err := submitHeld(t, s, c, checkRequest(s, false))
	if err != nil || resp.GetJob().GetId() == "" {
		t.Fatalf("Submit across a restart = %v, %v; want the job", resp, err)
	}
	c3 := s.connect(t)
	jobs, err := c3.ListJobs(ctxFor(t), &cliv1.ListJobsRequest{Context: call})
	if err != nil || len(jobs.GetJobs()) != 1 || jobs.GetJobs()[0].GetId() != resp.GetJob().GetId() {
		t.Fatalf("jobs after the restart = %v, %v; want exactly the submitted one", jobs.GetJobs(), err)
	}
	if _, err := c3.Wait(ctxFor(t), call, []string{resp.GetJob().GetId()}, client.WaitOptions{}); err != nil {
		t.Fatalf("waiting for the job: %v", err)
	}
}

// A submission that asks for a new job is not sent twice: the restart's answer says the
// submission may not be recorded.
func TestANoCacheSubmissionIsNotSentAgain(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	_, err := submitHeld(t, s, c, checkRequest(s, true))
	if !errors.Is(err, client.ErrSubmissionUnknown) {
		t.Fatalf("Submit with no_cache across a restart = %v, want ErrSubmissionUnknown (fingerprinted %d)", err, s.world.Env.Fingerprinted())
	}
	jobs, err := s.connect(t).ListJobs(ctxFor(t), &cliv1.ListJobsRequest{Context: call})
	if err != nil || len(jobs.GetJobs()) != 0 {
		t.Fatalf("jobs = %v, %v; want none", jobs.GetJobs(), err)
	}
}

// What a client did with the daemon it met.
type meeting string

const (
	sameBuild meeting = "same build"
	replaced  meeting = "replaced"
	refused   meeting = "refused with both versions"
	// broken is anything else: the test fails on it.
	broken meeting = "broken"
)

// work is what a daemon is doing when a client meets it.
type work string

const (
	// idle: no job and no submission.
	idle work = "idle"
	// runningJob: a job runs.
	runningJob work = "a running job"
	// submission: a submission waits for its environment's fingerprint, as a docker job's image
	// build and conversion makes it wait, for hours, before any job exists.
	submission work = "a submission in progress"
)

var works = []work{idle, runningJob, submission}

// holdSubmission submits a check job of the commit in the task and holds it at its environment's
// fingerprint until the test ends. It returns once the daemon has the submission.
func holdSubmission(t *testing.T, s *setup, c *client.Client, task string) {
	t.Helper()
	s.world.Tasks.Put("/repo", commit, tasktest.Files(task))
	s.world.Env.HoldFingerprint("image")
	at := &cliv1.CallContext{Repository: "/repo", Task: task, RunId: "run-1", Harness: "h", Agent: "agent-1"}
	done := make(chan struct{})
	go func() {
		defer close(done)
		// The answer is not the test's: the submission only has to be in progress.
		_, _ = c.Submit(ctxFor(t), &cliv1.SubmitRequest{Context: at, Spec: wire.JobSpec(core.JobSpec{
			Commit: commit, Environment: core.EnvironmentHost, Settings: core.CheckSettings{}})})
	}()
	t.Cleanup(func() {
		s.world.Gates.Release("image")
		<-done
	})
	if !s.world.Gates.Reached(ctxFor(t), fakeenv.FingerprintMark) {
		t.Fatal("the submission never reached the fingerprint")
	}
}

// meet starts a daemon of the build daemonBuild, gives it the work, and connects a client of the
// build clientBuild to it. The starter then starts clientBuild, as a client's own does.
func meet(t *testing.T, daemonBuild, clientBuild string, w work) meeting {
	t.Helper()
	s := newSetup(t)
	s.starter.Version = daemonBuild
	opts := s.options(true)
	opts.Version = daemonBuild
	first, err := client.Connect(ctxFor(t), opts)
	if err != nil {
		t.Fatalf("the daemon of build %q did not start: %v", daemonBuild, err)
	}
	switch w {
	case idle:
	case runningJob:
		job := submit(t, first, "print", "kept", ";", "hold", "g", ";", "exit", "0")
		if err := first.Logs(ctxFor(t), &cliv1.ReadLogsRequest{Context: call, JobId: job.GetId(), Follow: true},
			func(*cliv1.ReadLogsResponse) error { return errStop }); !errors.Is(err, errStop) {
			t.Fatal(err)
		}
		t.Cleanup(func() { s.world.Gates.Release("g") })
	case submission:
		holdSubmission(t, s, first, "task")
	}
	if w != submission {
		first.Close()
	} else {
		t.Cleanup(func() { first.Close() })
	}

	s.starter.Version = clientBuild
	opts.Version = clientBuild
	c, err := client.Connect(ctxFor(t), opts)
	if err != nil {
		var f *core.Failure
		if errors.As(err, &f) && f.Kind == core.FailureDaemonOtherBuild &&
			strings.Contains(f.Message, clientBuild) && strings.Contains(f.Message, daemonBuild) &&
			s.starter.Calls() == 1 {
			// The daemon was left as it was and still answers, with its job.
			kept, err := client.Dial(ctxFor(t), opts)
			if err != nil {
				return broken
			}
			defer kept.Close()
			st, err := kept.Status(ctxFor(t))
			if err != nil || st.GetVersions().GetBinaryVersion() != daemonBuild {
				return broken
			}
			return refused
		}
		t.Logf("client %q, daemon %q: %v", clientBuild, daemonBuild, err)
		return broken
	}
	defer c.Close()
	st, err := c.Status(ctxFor(t))
	switch {
	case err != nil || st.GetVersions().GetBinaryVersion() != clientBuild:
		return broken
	case s.starter.Calls() == 1:
		return sameBuild
	case s.starter.Calls() == 2:
		return replaced
	}
	return broken
}

// A client never uses a daemon of another build without saying so. Over pairs of build identities,
// release and local builds alike (the pair of the live finding is the first), and daemons that are
// idle, run a job, or work on a submission that has no job yet, exactly one thing happens: the same
// build is used, an idle daemon of another build is replaced by the client's, or a daemon of
// another build with work is refused with both versions and left running. A daemon that was
// building a submission's image for hours with no job yet used to count as idle and be replaced,
// which ended the build.
func TestAClientAndADaemonOfAnotherBuild(t *testing.T) {
	builds := []string{"0.0.1-live", "0.1.0-local.9342cd403", "dev", "0.1.0-local.9342cd404"}
	for _, clientBuild := range builds {
		for _, daemonBuild := range append([]string{""}, builds...) {
			for _, w := range works {
				name := fmt.Sprintf("client %s daemon %q with %s", clientBuild, daemonBuild, w)
				want := sameBuild
				switch {
				case clientBuild == daemonBuild:
				case w != idle:
					want = refused
				default:
					want = replaced
				}
				if got := meet(t, daemonBuild, clientBuild, w); got != want {
					t.Errorf("%s: %s, want %s", name, got, want)
				}
			}
		}
	}
}

// Dial addresses whichever daemon runs, so that status and stop work on a daemon of another build
// that has a job.
func TestDialReachesADaemonOfAnotherBuild(t *testing.T) {
	s := newSetup(t)
	s.starter.Version = "0.1.0-local.9342cd403"
	first, err := client.Connect(ctxFor(t), s.options(true))
	if err != nil {
		t.Fatal(err)
	}
	first.Close()
	opts := s.options(false)
	opts.Version = "0.0.1-live"
	c, err := client.Dial(ctxFor(t), opts)
	if err != nil {
		t.Fatalf("Dial: %v", err)
	}
	defer c.Close()
	if st, err := c.Status(ctxFor(t)); err != nil || st.GetVersions().GetBinaryVersion() != "0.1.0-local.9342cd403" {
		t.Fatalf("status = %v, %v", st, err)
	}
}

// A submission the daemon works on before its job exists, such as one whose image is built and
// converted for hours, is live work wherever the daemon's work is counted, beside the jobs of
// other tasks: the status counts it, and a restart, which would start it over, is refused unless
// forced. A restart once ended such a build when the status said "queued 0 running 0". Running
// jobs alone do not refuse a restart: they keep running under their supervisors.
func TestASubmissionInProgressIsLiveWorkThatARestartWaitsFor(t *testing.T) {
	for _, w := range works {
		t.Run(string(w), func(t *testing.T) {
			s := newSetup(t)
			c := s.connect(t)
			if w == runningJob || w == submission {
				// A job of another task runs beside the submission.
				job := submit(t, c, "print", "kept", ";", "hold", "g", ";", "exit", "0")
				if err := c.Logs(ctxFor(t), &cliv1.ReadLogsRequest{Context: call, JobId: job.GetId(), Follow: true},
					func(*cliv1.ReadLogsResponse) error { return errStop }); !errors.Is(err, errStop) {
					t.Fatal(err)
				}
				t.Cleanup(func() { s.world.Gates.Release("g") })
			}
			if w == submission {
				holdSubmission(t, s, c, "other-task")
			}
			st, err := c.Status(ctxFor(t))
			if err != nil {
				t.Fatal(err)
			}
			wantSubmitting, wantRunning := uint32(0), uint32(0)
			if w != idle {
				wantRunning = 1
			}
			if w == submission {
				wantSubmitting = 1
			}
			if st.GetSubmittingJobs() != wantSubmitting || st.GetRunningJobs() != wantRunning ||
				wire.LiveWork(st) != wantSubmitting+wantRunning+st.GetQueuedJobs() {
				t.Fatalf("status with %s = submitting %d queued %d running %d (live %d), want submitting %d running %d",
					w, st.GetSubmittingJobs(), st.GetQueuedJobs(), st.GetRunningJobs(), wire.LiveWork(st), wantSubmitting, wantRunning)
			}

			restarted, err := client.Restart(ctxFor(t), s.options(true), false)
			if w == submission {
				var f *core.Failure
				if !errors.As(err, &f) || f.Kind != core.FailureConflict || !strings.Contains(f.Message, "--force") {
					t.Fatalf("Restart with a submission in progress = %v, want a conflict that names --force", err)
				}
				if s.starter.Calls() != 1 {
					t.Fatalf("starts = %d, want the daemon left as it was", s.starter.Calls())
				}
				restarted, err = client.Restart(ctxFor(t), s.options(true), true)
			}
			if err != nil {
				t.Fatalf("Restart with %s: %v", w, err)
			}
			restarted.Close()
		})
	}
}
