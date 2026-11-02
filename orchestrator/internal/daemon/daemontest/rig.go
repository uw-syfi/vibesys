package daemontest

import (
	"context"
	"log/slog"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/jobs/jobstest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
)

// PollEvery is how often a Rig's daemon asks its job source.
const PollEvery = 10 * time.Second

// Rig is a whole daemon over Fakes, run in the test's own thread. Restart makes a new daemon over
// the same store and the same sidecars, as a crash and a start would.
type Rig struct {
	Store    *store.Memory
	Clock    *Clock
	Nonces   *Nonces
	Sidecars *Sidecars
	Jobs     *jobstest.FakeJobSource
	Plans    *Plans
	Hub      *hub.Hub
	Daemon   *daemon.Daemon
	Service  *daemon.Service
	Auth     daemon.Authority
	Log      *slog.Logger
	Limits   hub.Limits
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
		Jobs: jobstest.NewFakeJobSource(), Plans: NewPlans(), Auth: auth,
		Log: slog.New(slog.DiscardHandler), Limits: hub.DefaultLimits, Wake: make(chan struct{}, 1),
	}
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
	d, err := daemon.Open(daemon.Config{
		Engine: daemon.EngineDeps{
			Store: r.Store, Clock: r.Clock, Nonces: r.Nonces, Hub: r.Hub, Log: r.Log,
			OnCommit: func() {
				select {
				case r.Wake <- struct{}{}:
				default:
				}
			},
		},
		Sidecars: r.Sidecars, Jobs: r.Jobs, Authority: r.Auth, Socket: "/run/orchestrator.sock",
		JobPollEvery: PollEvery,
	})
	if err != nil {
		tb.Fatal(err)
	}
	r.Daemon = d
	r.Service = daemon.NewService(d.Engine, r.Plans, r.Auth, "test")
	if err := d.Reattach(context.Background()); err != nil {
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
