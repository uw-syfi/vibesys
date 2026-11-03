package daemontest

import (
	"context"
	"errors"
	"log/slog"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs/jobstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// AgentSocket is the path a Rig's sessions are told. The operator's socket is not known to the
// daemon at all.
const AgentSocket = "/run/orchestrator-agent.sock"

// PollEvery is how often a Rig's daemon asks its job source.
const PollEvery = 10 * time.Second

// Rig is a whole daemon over Fakes, run in the test's own thread. Restart makes a new daemon over
// the same store and the same sidecars, as a crash and a start would.
type Rig struct {
	// Store is the memory store, and Faults the layer over it that the daemon is given.
	Store    *store.Memory
	Faults   *FaultStore
	Bounds   *Bounds
	Clock    *Clock
	Nonces   *Nonces
	Sidecars *Sidecars
	Jobs     *jobstest.FakeJobSource
	Plans    *Plans
	Hub      *hub.Hub
	Daemon   *daemon.Daemon
	// Service is the operator's service, and AgentService the agent socket's.
	Service      *daemon.Service
	AgentService *daemon.Service
	Auth         daemon.Authority
	Log          *slog.Logger
	Limits       hub.Limits
	// AgentCLI is the vibesys-cli the daemon tells its agents to run, read at each Restart. Empty
	// names none.
	AgentCLI string
	// JobStall makes that many polls of the job source hang until their bound ends.
	JobStall int
	// ReattachErr is what the last restart's Reattach reported.
	ReattachErr error
	// Wake is signalled by every commit, for a daemon run with Daemon.Run.
	Wake chan struct{}
}

// NewRig starts a daemon.
func NewRig(tb testing.TB) *Rig {
	tb.Helper()
	auth, err := daemon.NewAuthority([]byte("0123456789abcdef-test-secret"))
	if err != nil {
		tb.Fatal(err)
	}
	r := &Rig{
		Store: store.NewMemory(), Clock: NewClock(), Nonces: &Nonces{}, Sidecars: NewSidecars(),
		Jobs: jobstest.NewFakeJobSource(), Plans: NewPlans(), Auth: auth, Bounds: &Bounds{},
		Log: slog.New(slog.DiscardHandler), Limits: hub.DefaultLimits, Wake: make(chan struct{}, 1),
	}
	r.Sidecars.Elapse = r.Bounds.Expire
	r.Plans.Elapse = r.Bounds.Expire
	r.Sidecars.Wake = func() {
		select {
		case r.Wake <- struct{}{}:
		default:
		}
	}
	r.Restart(tb)
	return r
}

// Restart replaces the daemon with a fresh one over what was committed. Clients and watchers of the
// old one are gone.
func (r *Rig) Restart(tb testing.TB) {
	tb.Helper()
	if r.Daemon != nil {
		r.Store = r.Store.Reopen()
	}
	r.Hub = hub.New(r.Limits)
	if r.Faults == nil {
		r.Faults = &FaultStore{}
	}
	r.Faults.mu.Lock()
	r.Faults.Store, r.Faults.CrashAt = r.Store, "" // a new process has no crash armed.
	r.Faults.mu.Unlock()
	r.Sidecars.mu.Lock()
	r.Sidecars.AfterSend = nil
	r.Sidecars.mu.Unlock()
	d, err := daemon.Open(daemon.Config{
		Engine: daemon.EngineDeps{
			Store: r.Faults, Clock: r.Clock, Nonces: r.Nonces, Hub: r.Hub, Log: r.Log,
			OnCommit: func() {
				select {
				case r.Wake <- struct{}{}:
				default:
				}
			},
		},
		Sidecars: r.Sidecars, Jobs: gatedJobs{r}, Authority: r.Auth, Bounds: r.Bounds, AgentSocket: AgentSocket, AgentCLI: r.AgentCLI,
		JobPollEvery: PollEvery,
	})
	if err != nil {
		tb.Fatal(err)
	}
	r.Daemon = d
	for audience, service := range map[daemon.Audience]**daemon.Service{daemon.AudienceOperator: &r.Service, daemon.AudienceAgent: &r.AgentService} {
		if *service, err = daemon.NewService(d.Engine, daemon.ServiceDeps{
			Plans: r.Plans, Authority: r.Auth, Audience: audience, Bounds: r.Bounds, Binary: "test",
		}); err != nil {
			tb.Fatal(err)
		}
	}
	// Reattach reports the calls that failed or ran out of time and leaves them to the next step.
	r.ReattachErr = d.Reattach(context.Background())
	if err := r.ReattachErr; err != nil && !errors.Is(err, daemon.ErrExpired) && !errors.Is(err, ErrInjected) {
		tb.Fatal(err)
	}
}

// Settle steps the daemon until a step moves nothing, ending the merge window each time. It fails
// the test if the daemon never settles.
func (r *Rig) Settle(tb testing.TB) {
	tb.Helper()
	for range 10000 {
		r.Hub.Flush()
		moved, err := r.Daemon.Step(context.Background())
		if err != nil {
			tb.Fatalf("step: %v", err)
		}
		if !moved {
			return
		}
	}
	tb.Fatal("the daemon never settled")
}

// gatedJobs is the Rig's job source: the Fake, unless a stall is set.
type gatedJobs struct{ r *Rig }

func (g gatedJobs) Jobs(ctx context.Context, run core.Run) ([]core.JobUpdate, error) {
	if g.r.JobStall > 0 {
		g.r.JobStall--
		if ctx.Done() == nil {
			return nil, errors.New("fake jobs: a call that hangs was made with no bound")
		}
		g.r.Bounds.Expire()
		<-ctx.Done()
		return nil, ctx.Err()
	}
	return g.r.Jobs.Jobs(ctx, run)
}
