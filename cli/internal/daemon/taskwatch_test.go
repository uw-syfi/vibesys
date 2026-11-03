package daemon_test

import (
	"context"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// boot is one daemon boot over stores that outlive it, reached through an in-memory connection.
type boot struct {
	client cliv1.DaemonServiceClient
	app    *app.App
	stop   func()
}

// serveOver starts a daemon of the given epoch over stores. Stopping it ends its streams, as the
// death of the process would, and leaves the stores for the next boot.
func serveOver(t *testing.T, stores app.Stores, epoch string) *boot {
	t.Helper()
	gates := fakeenv.NewMemoryGates()
	env := fakeenv.New(core.EnvironmentHost, gates)
	survivors := supervisor.NewSurvivors()
	a := app.New(app.Config{
		Timers:       clock.Immediate{},
		Clock:        clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond),
		Stores:       stores,
		Environments: fakeenv.Environments{core.EnvironmentHost: env},
		Supervisors:  supervisor.NewInProcess(fakeenv.Environments{core.EnvironmentHost: env}, &fakeenv.Checkouts{}, survivors),
		Repositories: repos(),
		Epoch:        epoch,
	})
	if err := a.Start(ctxFor(t)); err != nil {
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test")
	listener := bufconn.Listen(1 << 20)
	gs := grpc.NewServer()
	cliv1.RegisterDaemonServiceServer(gs, srv)
	go gs.Serve(listener)
	conn, err := grpc.NewClient("passthrough:///daemon",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	var stopped bool
	stop := func() {
		if stopped {
			return
		}
		stopped = true
		srv.Shutdown()
		gs.Stop()
		conn.Close()
		a.Close()
		survivors.Close()
	}
	t.Cleanup(stop)
	return &boot{client: cliv1.NewDaemonServiceClient(conn), stop: stop, app: a}
}

// recvReset reads the messages up to CaughtUp and returns them.
func recvReset(t *testing.T, stream cliv1.DaemonService_WatchTaskClient) []*cliv1.WatchTaskResponse {
	t.Helper()
	var out []*cliv1.WatchTaskResponse
	for {
		m, err := stream.Recv()
		if err != nil {
			t.Fatalf("Recv: %v", err)
		}
		out = append(out, m)
		if m.GetCaughtUp() != nil {
			return out
		}
	}
}

func factsOf(msgs []*cliv1.WatchTaskResponse) []*cliv1.TaskFact {
	var out []*cliv1.TaskFact
	for _, m := range msgs {
		out = append(out, m.GetFacts().GetFacts()...)
	}
	return out
}

func TestHelloAcceptsTheCommandLineAndTheOrchestratorAndNothingElse(t *testing.T) {
	b := serveOver(t, memstores.New(app.Rules(5)), "E")
	wants := map[cliv1.Component]bool{
		cliv1.Component_COMPONENT_CLI: true, cliv1.Component_COMPONENT_ORCHESTRATOR: true,
		cliv1.Component_COMPONENT_UNSPECIFIED: false, cliv1.Component_COMPONENT_DAEMON: false,
		cliv1.Component_COMPONENT_SUPERVISOR: false, cliv1.Component(99): false,
	}
	for component, ok := range wants {
		_, err := b.client.Hello(ctxFor(t), &cliv1.HelloRequest{Component: component, Versions: wire.BuildVersions("t", core.ClientVersions())})
		f, failed := wire.FailureOf(err)
		if ok && err != nil || !ok && (!failed || f.Kind != core.FailureInvalidInput) {
			t.Errorf("Hello(%s) = %v (%+v), want accepted %v", component, err, f, ok)
		}
	}
}

// The orchestrator names the protocol WatchTask needs as its minimum, and this daemon serves it;
// a client older than the daemon's minimum is told to upgrade, whoever it is.
func TestHelloToAnOrchestratorAndToAnOldClient(t *testing.T) {
	b := serveOver(t, memstores.New(app.Rules(5)), "E")
	orchestrator := core.Versions{Protocol: core.WatchTaskSince, MinProtocol: core.WatchTaskSince, Schema: core.SchemaVersion}
	resp, err := b.client.Hello(ctxFor(t), &cliv1.HelloRequest{Component: cliv1.Component_COMPONENT_ORCHESTRATOR, Versions: wire.BuildVersions("o", orchestrator)})
	if err != nil || resp.GetCompatibility() != cliv1.Compatibility_COMPATIBILITY_COMPATIBLE {
		t.Fatalf("Hello of an orchestrator that needs protocol %d = %v, %v", core.WatchTaskSince, resp.GetCompatibility(), err)
	}
	// An orchestrator that needs a protocol this daemon does not speak is told the daemon is too old.
	future := core.Versions{Protocol: core.ProtocolVersion + 1, MinProtocol: core.ProtocolVersion + 1, Schema: core.SchemaVersion}
	resp, err = b.client.Hello(ctxFor(t), &cliv1.HelloRequest{Component: cliv1.Component_COMPONENT_ORCHESTRATOR, Versions: wire.BuildVersions("o", future)})
	if err != nil || resp.GetCompatibility() != cliv1.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD {
		t.Fatalf("Hello of a client that needs a newer protocol = %v, %v; want daemon too old", resp.GetCompatibility(), err)
	}
	old := core.Versions{Protocol: core.MinProtocolVersion - 1, MinProtocol: 0, Schema: core.SchemaVersion}
	resp, err = b.client.Hello(ctxFor(t), &cliv1.HelloRequest{Component: cliv1.Component_COMPONENT_ORCHESTRATOR, Versions: wire.BuildVersions("o", old)})
	if err != nil || resp.GetCompatibility() != cliv1.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD {
		t.Fatalf("Hello of a client below the daemon's minimum = %v, %v; want client too old", resp.GetCompatibility(), err)
	}
}

func TestWatchTaskStartsWithAResetAndNumbersItsMessagesWithoutGaps(t *testing.T) {
	b := serveOver(t, memstores.New(app.Rules(5)), "E-one")
	stream, err := b.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	msgs := recvReset(t, stream)
	if msgs[0].GetReset_() == nil {
		t.Fatalf("the first message is %v, want a Reset", msgs[0])
	}
	for i, m := range msgs {
		if m.GetSequence() != uint64(i+1) || m.GetEpoch() != "E-one" {
			t.Fatalf("message %d has sequence %d epoch %q, want %d and E-one", i, m.GetSequence(), m.GetEpoch(), i+1)
		}
	}
	var health, head bool
	for _, f := range factsOf(msgs) {
		health = health || f.GetHealth().GetLive() != nil
		head = head || f.GetHead().GetNone() != nil
	}
	if !health || !head {
		t.Fatalf("the reset of a task with nothing says health live %v and no head %v", health, head)
	}
}

// Protocol 3 ignores the token a client holds and answers every connection with a reset.
func TestWatchTaskIgnoresTheResumeTokenAndResets(t *testing.T) {
	b := serveOver(t, memstores.New(app.Rules(5)), "E-one")
	first, err := b.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	msgs := recvReset(t, first)
	token := msgs[len(msgs)-1]
	second, err := b.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call, Epoch: token.GetEpoch(), AfterSequence: token.GetSequence()})
	if err != nil {
		t.Fatal(err)
	}
	again := recvReset(t, second)
	if again[0].GetReset_() == nil || again[0].GetSequence() != 1 {
		t.Fatalf("a connection with a token starts with %v, want a Reset at sequence 1", again[0])
	}
}

func TestWatchTaskRefusesToJudgeMoreThanTheMost(t *testing.T) {
	b := serveOver(t, memstores.New(app.Rules(5)), "E")
	for _, tc := range []struct {
		asked uint32
		ok    bool
	}{{0, true}, {100, true}, {101, false}} {
		stream, err := b.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call, JudgedCommits: tc.asked})
		if err == nil {
			_, err = stream.Recv()
		}
		if tc.ok != (err == nil) || (!tc.ok && status.Code(err) != codes.InvalidArgument) {
			t.Errorf("judged_commits %d: %v, want accepted %v", tc.asked, err, tc.ok)
		}
	}
}

func TestWatchTaskTellsOfAPlanMadeAfterTheReset(t *testing.T) {
	b := serveOver(t, memstores.New(app.Rules(5)), "E")
	stream, err := b.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	recvReset(t, stream)
	if _, err := b.client.CreatePlan(ctxFor(t), &cliv1.CreatePlanRequest{Context: call, Title: "try it", Body: "body"}); err != nil {
		t.Fatal(err)
	}
	for {
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if m.GetReset_() != nil {
			t.Fatalf("a new plan was sent as a reset: %v", m)
		}
		for _, f := range m.GetFacts().GetFacts() {
			if f.GetPlan().GetTitle() == "try it" {
				return
			}
		}
	}
}

// A daemon that restarts has the same stores and a new epoch. The stream ends with the daemon, and
// the client that connects again is sent the whole state from sequence 1.
func TestWatchTaskAfterTheDaemonRestartsMidStream(t *testing.T) {
	stores := memstores.New(app.Rules(5))
	one := serveOver(t, stores, "E-one")
	stream, err := one.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call})
	if err != nil {
		t.Fatal(err)
	}
	recvReset(t, stream)
	if _, err := one.client.CreatePlan(ctxFor(t), &cliv1.CreatePlanRequest{Context: call, Title: "kept", Body: "b"}); err != nil {
		t.Fatal(err)
	}
	seen := false
	for !seen {
		m, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		for _, f := range m.GetFacts().GetFacts() {
			seen = seen || f.GetPlan().GetTitle() == "kept"
		}
	}
	one.stop()
	if _, err := stream.Recv(); err == nil {
		t.Fatal("the stream of a daemon that stopped went on")
	}

	two := serveOver(t, stores, "E-two")
	next, err := two.client.WatchTask(ctxFor(t), &cliv1.WatchTaskRequest{Context: call, Epoch: "E-one", AfterSequence: 99})
	if err != nil {
		t.Fatal(err)
	}
	msgs := recvReset(t, next)
	if msgs[0].GetReset_() == nil || msgs[0].GetSequence() != 1 || msgs[0].GetEpoch() != "E-two" {
		t.Fatalf("after a restart the stream starts with %v, want a Reset at sequence 1 of epoch E-two", msgs[0])
	}
	kept := false
	for _, f := range factsOf(msgs) {
		kept = kept || f.GetPlan().GetTitle() == "kept"
	}
	if !kept {
		t.Fatal("the plan made before the restart is not in the state sent after it")
	}
}
