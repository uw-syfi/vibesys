package clientcli_test

import (
	"bytes"
	"context"
	"fmt"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"testing/fstest"
	"time"

	"google.golang.org/grpc"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// guard bounds a wait that only a hang would make long.
const guard = 30 * time.Second

// fakeClock is a clock whose timers fire when the test says so. Now stands still.
type fakeClock struct {
	now    time.Time
	mu     sync.Mutex
	timers []chan time.Time
	// armed receives once for every timer a verb starts, so a test fires one it knows exists.
	armed chan time.Duration
}

func newFakeClock(now time.Time) *fakeClock {
	return &fakeClock{now: now, armed: make(chan time.Duration, 16)}
}

func (c *fakeClock) Now() time.Time { return c.now }

func (c *fakeClock) After(d time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	c.mu.Lock()
	c.timers = append(c.timers, ch)
	c.mu.Unlock()
	select {
	case c.armed <- d:
	default: // Nobody is waiting on this many: the signal that one exists has been given.
	}
	return ch
}

// fire ends every timer started so far, once a verb has started at least one.
func (c *fakeClock) fire(t testing.TB) {
	t.Helper()
	select {
	case <-c.armed:
	case <-time.After(guard):
		t.Fatal("no verb started a timer")
	}
	c.fireAll()
}

// fireAll ends every timer started so far.
func (c *fakeClock) fireAll() {
	c.mu.Lock()
	defer c.mu.Unlock()
	for _, ch := range c.timers {
		ch <- c.now
	}
	c.timers = nil
}

// counterIDs mints command ids in order.
type counterIDs struct {
	mu sync.Mutex
	n  int
}

func (c *counterIDs) NewCommandID() string {
	c.mu.Lock()
	defer c.mu.Unlock()
	c.n++
	return fmt.Sprintf("cmd-%d", c.n)
}

// tripwire is a reader that fails the test when anything reads it: stdin of a process with no
// terminal, which must never be prompted.
type tripwire struct{ t testing.TB }

func (r tripwire) Read([]byte) (int, error) {
	r.t.Error("a verb read stdin, which has no terminal behind it")
	return 0, io.EOF
}

// result is what one command line did.
type result struct {
	code           int
	stdout, stderr string
}

// world is a command line's surroundings: a service to dial, a file system and a working directory.
type world struct {
	t     testing.TB
	dial  client.Dialer
	files fstest.MapFS
	dir   string
	// links are the symbolic links of the file system, by the path of each: from to its target.
	links    map[string]string
	terminal bool
	stdin    io.Reader
	clock    *fakeClock
	ids      *counterIDs
	// onStdout, when set, sees stdout as it is written.
	onStdout func(written string)
	// onStderr, when set, sees stderr as it is written.
	onStderr func(written string)
	// configs, when set, is the host's configuration in every root.
	configs clientcli.HostConfigs
}

func newWorld(t testing.TB, dial client.Dialer) *world {
	return &world{
		t: t, dial: dial, dir: "/work/repo", ids: &counterIDs{}, stdin: tripwire{t},
		files: fstest.MapFS{"work/repo/.git": {}},
		clock: newFakeClock(time.Date(2026, 1, 2, 3, 4, 5, 0, time.UTC)),
	}
}

type watchedBuffer struct {
	bytes.Buffer
	on func(string)
}

func (b *watchedBuffer) Write(p []byte) (int, error) {
	n, err := b.Buffer.Write(p)
	if b.on != nil {
		b.on(b.String())
	}
	return n, err
}

// run runs one command line, with a bound that only a hang would reach.
func (w *world) run(ctx context.Context, args ...string) result {
	w.t.Helper()
	ctx, cancel := context.WithTimeout(ctx, guard)
	defer cancel()
	return w.runUnbounded(ctx, args...)
}

func (w *world) runUnbounded(ctx context.Context, args ...string) result {
	errb := &watchedBuffer{on: w.onStderr}
	stdout := &watchedBuffer{on: w.onStdout}
	env := clientcli.Env{
		Version: "test", Stdin: w.stdin, Stdout: stdout, Stderr: errb, Terminal: w.terminal,
		Dir: w.dir, Home: "/home/u", Configs: w.configs, FS: w.files, Canonical: w.canonical, Zone: time.UTC, Clock: w.clock, IDs: w.ids,
		// No host answers HostControlService here, so a verb starts one, which is Connect.
		Connect: func(string) (clientcli.Dial, error) { return w.dial, nil },
		Locate: func(string) (clientcli.Dial, error) {
			return func(context.Context) (net.Conn, error) { return nil, clientcli.ErrNoHost }, nil
		},
	}
	code := clientcli.Main(ctx, args, env)
	return result{code: code, stdout: stdout.String(), stderr: errb.String()}
}

// runWaitingToTimeOut runs a command line that waits, fires its timer once it has begun to wait
// (it has then read the snapshot), and returns its result.
func (w *world) runWaitingToTimeOut(args ...string) result {
	w.t.Helper()
	began := make(chan struct{})
	var once sync.Once
	w.onStderr = func(written string) {
		if strings.Contains(written, "waiting for") {
			once.Do(func() { close(began) })
		}
	}
	done := make(chan result, 1)
	go func() { done <- w.run(context.Background(), args...) }()
	select {
	case <-began:
	case <-time.After(guard):
		w.t.Fatal("the wait never began")
	}
	w.clock.fire(w.t)
	return <-done
}

// canonical follows w.links from the longest leading part of path that is one, as the file system does.
func (w *world) canonical(path string) string {
	for range len(w.links) + 1 {
		moved := false
		for from, to := range w.links {
			if path == from || strings.HasPrefix(path, from+"/") {
				path, moved = to+strings.TrimPrefix(path, from), true
				break
			}
		}
		if !moved {
			return path
		}
	}
	return path
}

// live is a daemon in memory with a client on its operator socket.
type live struct {
	*daemontest.Live
	op *client.Client
}

func newLive(t *testing.T, react daemontest.Reaction) *live {
	t.Helper()
	rig := daemontest.NewRig(t)
	if react != nil {
		rig.Sidecars.React = react
	}
	l := rig.Serve(t)
	ctx, cancel := context.WithTimeout(context.Background(), guard)
	defer cancel()
	op, err := client.Dial(ctx, l.Listener.Dial, client.Options{BinaryVersion: "test"})
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { op.Close() })
	return &live{Live: l, op: op}
}

// startRun starts a run directly through the service, waits for it to be running, and returns its
// id and lead.
func (l *live) startRun(t testing.TB, repository, task string) (run, lead string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), guard)
	defer cancel()
	receipt, err := l.op.Dispatch(ctx, &pb.Command{Id: "start-" + task + repository, Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
		Repository: repository, Task: task, FanoutLimit: 2,
		Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
	}}})
	if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("start: %v %v", receipt, err)
	}
	l.awaitRun(t, receipt.GetRunId(), hasStatus(pb.RunStatus_RUN_STATUS_RUNNING))
	state, _ := l.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	return receipt.GetRunId(), string(state.Lead)
}

// postReport posts a progress report as the run's lead, through the agent socket.
func (l *live) postReport(t testing.TB, lead, headline string) {
	t.Helper()
	l.postReportOf(t, lead, headline, pb.ReportKind_REPORT_KIND_PROGRESS)
}

// postReportOf posts a report of a kind as the run's lead.
func (l *live) postReportOf(t testing.TB, lead, headline string, kind pb.ReportKind) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), guard)
	defer cancel()
	for _, s := range l.Sidecars.Sessions() {
		if string(s.Launch.Session.Agent) != lead {
			continue
		}
		agent, err := client.Dial(ctx, l.AgentListener.Dial, client.Options{Session: &client.Session{Agent: s.Credentials.GetAgent(), Session: s.Credentials.GetSession(), Token: s.Credentials.GetToken()}})
		if err != nil {
			t.Fatal(err)
		}
		defer agent.Close()
		receipt, err := agent.Dispatch(ctx, &pb.Command{Id: "report-" + headline, Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: &pb.Report{Kind: kind, Headline: headline}}}})
		if err != nil || receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
			t.Fatalf("report: %v %v", receipt, err)
		}
		return
	}
	t.Fatalf("lead %s has no session", lead)
}

// awaitStatus waits on the stream, with no timer, until the run has the status.
func (l *live) awaitRun(t testing.TB, run string, want func(*pb.Run) bool) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), guard)
	defer cancel()
	stream, err := l.op.WatchRuns(ctx, 0)
	if err != nil {
		t.Fatal(err)
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatalf("waiting for the run: %v", err)
		}
		snapshot, event, _ := client.DecodeWatchRuns(frame)
		for _, r := range snapshot.GetRuns() {
			if r.GetId() == run && want(r) {
				return
			}
		}
		if r := event.GetRun(); r.GetId() == run && want(r) {
			return
		}
	}
}

func hasStatus(s pb.RunStatus) func(*pb.Run) bool {
	return func(r *pb.Run) bool { return r.GetStatus() == s }
}

// fakeService is an OrchestratorService that says what a test scripts: a peer for the cases a
// real daemon cannot be made to show, such as a truncated snapshot or a handshake it refuses.
type fakeService struct {
	pb.UnimplementedOrchestratorServiceServer
	hello    func() (*pb.HelloResponse, error)
	frames   []*pb.WatchRunsResponse
	dispatch func(*pb.Command) (*pb.DispatchResponse, error)

	mu       sync.Mutex
	commands []*pb.Command
}

func (f *fakeService) Hello(context.Context, *pb.HelloRequest) (*pb.HelloResponse, error) {
	if f.hello != nil {
		return f.hello()
	}
	return &pb.HelloResponse{Daemon: &pb.BuildVersions{BinaryVersion: "fake", ProtocolVersion: 1, MinProtocolVersion: 1}, Compatibility: pb.Compatibility_COMPATIBILITY_COMPATIBLE}, nil
}

// WatchRuns sends the scripted frames and then keeps the stream open until the client leaves.
func (f *fakeService) WatchRuns(_ *pb.WatchRunsRequest, stream grpc.ServerStreamingServer[pb.WatchRunsResponse]) error {
	for _, frame := range f.frames {
		if err := stream.Send(frame); err != nil {
			return err
		}
	}
	<-stream.Context().Done()
	return nil
}

func (f *fakeService) Dispatch(_ context.Context, req *pb.DispatchRequest) (*pb.DispatchResponse, error) {
	f.mu.Lock()
	f.commands = append(f.commands, req.GetCommand())
	f.mu.Unlock()
	if f.dispatch != nil {
		return f.dispatch(req.GetCommand())
	}
	return &pb.DispatchResponse{Receipt: &pb.Receipt{CommandId: req.GetCommand().GetId()}}, nil
}

func (f *fakeService) sent() []*pb.Command {
	f.mu.Lock()
	defer f.mu.Unlock()
	return append([]*pb.Command(nil), f.commands...)
}

// serveFake serves a fakeService and returns the way to dial it.
func serveFake(t testing.TB, f *fakeService) client.Dialer {
	t.Helper()
	listener := daemontest.NewPipeListener()
	server := grpc.NewServer()
	pb.RegisterOrchestratorServiceServer(server, f)
	done := make(chan struct{})
	go func() {
		defer close(done)
		_ = server.Serve(listener)
	}()
	t.Cleanup(func() {
		server.Stop()
		_ = listener.Close()
		<-done
	})
	return func(ctx context.Context) (net.Conn, error) { return listener.Dial(ctx) }
}

func contains(t testing.TB, what, text string, wants ...string) {
	t.Helper()
	for _, want := range wants {
		if !strings.Contains(text, want) {
			t.Errorf("%s = %q, want it to contain %q", what, text, want)
		}
	}
}
