package daemon_test

import (
	"context"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"google.golang.org/grpc"
	"sync/atomic"
	"testing"
	"time"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// scenarioOperation is closed by the runner's rejecting switch. Each consumer
// must implement every operation, including restart and ownership transitions.
type scenarioOperation string

type corpusStores struct {
	*memstores.Stores
	away    atomic.Bool
	recover bool
}

func (s *corpusStores) Known(ctx context.Context) ([]core.Scope, error) {
	if !s.recover {
		return nil, nil
	}
	return s.Stores.Known(ctx)
}
func (s *corpusStores) Find(ctx context.Context, scope core.Scope) (store.Store, error) {
	if s.away.Load() {
		return nil, store.Mark(&core.Failure{Kind: core.FailureConflict, Message: "store owned elsewhere"}, store.ErrOwnedElsewhere, store.ErrAway)
	}
	return s.Stores.Find(ctx, scope)
}

// Lifecycle operations run through the public Store API, with the real daemon
// serving every transcript. Reconnect after each operation records an atomic
// reset, including ownership return and crash recovery. The existing scenarios
// separately pin live deltas.
func runLifecycleScenario(t *testing.T, sc scenario) [][]map[string]any {
	stores := &corpusStores{Stores: memstores.New(app.Rules(5))}
	scope := mustScope(t, "/repo", "task")
	st, err := stores.Create(t.Context(), scope)
	if err != nil {
		t.Fatal(err)
	}
	at := time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC)
	b := serveOver(t, stores, "E1")
	var streams [][]map[string]any
	play := func(op scenarioOp) {
		at = at.Add(time.Second)
		switch op.Op {
		case "submit":
			out, err := st.Submit(t.Context(), core.Submission{
				Context: core.CallContext{Scope: scope, Run: core.RunID(op.Run), Agent: op.Agent},
				Spec:    core.JobSpec{Commit: commitOf(t, op.Commit), Environment: core.EnvironmentHost, Settings: core.CheckSettings{}},
				Key:     "corpus-check", At: at})
			if err != nil {
				t.Fatal(err)
			}
			_ = out
		case "start":
			env := storetest.HostEnvironment()
			_, err = st.Transition(t.Context(), core.Transition{Job: core.JobID(op.Job), From: core.JobQueued, To: core.JobRunning, Environment: &env, At: at})
		case "finish":
			job, getErr := st.Job(t.Context(), core.JobID(op.Job))
			if getErr != nil {
				t.Fatal(getErr)
			}
			result := core.Result{Job: job.ID, Key: job.Key, Task: "task", TaskVersion: "v1", Commit: job.Spec.Commit,
				SchemaVersion: core.SchemaVersion, Environment: *job.Environment, RecordedAt: at, Outcome: core.CheckOutcome{Duration: time.Second}}
			_, err = st.Transition(t.Context(), core.Transition{Job: job.ID, From: core.JobRunning, To: core.JobSucceeded, At: at, Result: &result})
		case "create_plans":
			for i := uint32(0); i < op.Number; i++ {
				// The plan is made by the caller the other scenarios have, so a reader's model of a plan is one.
				_, err = st.CreatePlan(t.Context(), core.NewPlan{Title: op.Title, Origin: core.RecordOrigin{Run: core.RunID(call.GetRunId()), Agent: call.GetAgent(), At: at}})
				if err != nil {
					t.Fatal(err)
				}
			}
		case "cancel":
			_, err = st.Transition(t.Context(), core.Transition{Job: core.JobID(op.Job), From: core.JobRunning, To: core.JobCancelled, At: at, CancelReason: "cancelled by corpus"})
		case "owner_loss":
			stores.away.Store(true)
		case "owner_return":
			stores.away.Store(false)
		case "crash_reopen":
			b.stop()
			stores.recover = true
			b = serveOver(t, stores, "E2")
		case "forget":
			err = stores.Forget(t.Context(), scope)
		default:
			t.Fatalf("unknown lifecycle operation %q", op.Op)
		}
		if err != nil {
			t.Fatalf("%s: %v", op.Op, err)
		}
	}
	during := false
	capture := func(cut int) {
		ctx, cancel := context.WithCancel(ctxFor(t))
		packets := make(chan corpusPacket)
		stream := &gatedTaskStream{ctx: ctx, packets: packets}
		server := daemon.NewServer(b.app, "test", daemon.Inherited{})
		done := make(chan error, 1)
		go func() { done <- server.WatchTask(&cliv1.WatchTaskRequest{Context: call}, stream) }()
		defer func() { cancel(); <-done }()
		var messages []map[string]any
		for {
			var packet corpusPacket
			select {
			case packet = <-packets:
			case <-ctx.Done():
				t.Fatal("stream hung")
			}
			m := packet.message
			messages = append(messages, normalized(m))
			// Send remains blocked on the packet's gate while the write lands. No
			// scheduler delay or transport buffering decides this interleaving.
			if cut == 0 && !during && len(sc.DuringReset) > 0 && len(messages) == sc.AfterMessages {
				during = true
				for _, op := range sc.DuringReset {
					play(op)
				}
			}
			close(packet.release)
			if len(messages) == cut || m.GetCaughtUp() != nil {
				break
			}
		}
		streams = append(streams, messages)
	}
	for _, op := range sc.Setup {
		play(op)
	}
	for _, cut := range sc.Cuts {
		capture(cut)
	}
	capture(0)
	for _, op := range sc.Ops {
		play(op)
		capture(0)
	}
	return streams
}

type corpusPacket struct {
	message *cliv1.WatchTaskResponse
	release chan struct{}
}
type gatedTaskStream struct {
	grpc.ServerStream
	ctx     context.Context
	packets chan<- corpusPacket
}

func (s *gatedTaskStream) Context() context.Context { return s.ctx }
func (s *gatedTaskStream) Send(m *cliv1.WatchTaskResponse) error {
	packet := corpusPacket{message: m, release: make(chan struct{})}
	select {
	case s.packets <- packet:
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
	select {
	case <-packet.release:
		return nil
	case <-s.ctx.Done():
		return s.ctx.Err()
	}
}
