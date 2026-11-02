package client_test

import (
	"context"
	"errors"
	"net"
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
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
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
		Commit: commit, Environment: core.EnvironmentHost, Settings: core.CommandSettings{Argv: argv}})})
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
	if !res.Done || res.Succeeded || res.TimedOut {
		t.Fatalf("result = %+v, want done, not succeeded", res)
	}
	if len(res.Jobs) != 2 || res.Jobs[0].GetId() != a.GetId() || res.Jobs[1].GetId() != b.GetId() {
		t.Fatalf("jobs = %v, want both in the order asked", res.Jobs)
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
	if !res.Done || !res.Succeeded {
		t.Fatalf("result = %+v, want done and succeeded", res)
	}
	s.world.Gates.Release("g")
}

// A timeout ends the wait and nothing else: the job goes on and succeeds.
func TestAWaitTimeoutNeverCancelsTheJob(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	job := submit(t, c, "hold", "g", ";", "exit", "0")

	waited := make(chan client.WaitResult, 1)
	go func() {
		res, err := c.Wait(ctxFor(t), call, []string{job.GetId()}, client.WaitOptions{Timeout: 5 * time.Second})
		if err != nil {
			t.Errorf("Wait: %v", err)
		}
		waited <- res
	}()
	if d := <-s.timers.Armed(); d != 5*time.Second {
		t.Fatalf("the timeout armed was %v, want 5s", d)
	}
	s.timers.Fire()
	res := <-waited
	if !res.TimedOut || res.Done {
		t.Fatalf("result = %+v, want timed out", res)
	}

	s.world.Gates.Release("g")
	again, err := c.Wait(ctxFor(t), call, []string{job.GetId()}, client.WaitOptions{})
	if err != nil || !again.Done || !again.Succeeded {
		t.Fatalf("the job after a timed-out wait: %+v, %v; want it to have succeeded", again, err)
	}
}

func TestWaitOnAnUnknownJobFailsWithNotFound(t *testing.T) {
	s := newSetup(t)
	c := s.connect(t)
	_, err := c.Wait(ctxFor(t), call, []string{"j404"}, client.WaitOptions{})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureNotFound {
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

	c2, err := client.Restart(ctxFor(t), s.options(true))
	if err != nil {
		t.Fatalf("Restart: %v", err)
	}
	defer c2.Close()
	if got := s.starter.Calls(); got != 2 {
		t.Fatalf("starts = %d, want 2", got)
	}
	s.world.Gates.Release("g")
	res, err := c2.Wait(ctxFor(t), call, []string{job.GetId()}, client.WaitOptions{})
	if err != nil || !res.Done || !res.Succeeded {
		t.Fatalf("the job after a restart: %+v, %v; want it to have succeeded", res, err)
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
