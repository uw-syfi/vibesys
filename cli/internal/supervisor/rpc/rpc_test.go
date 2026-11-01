package rpc_test

import (
	"context"
	"net"
	"sync"
	"testing"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/rpc"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor/suptest"
)

// remote is Supervisors whose supervisors are reached through gRPC over an in-memory connection.
// It is the rpc package's own test double: it plays the part of the process that serves a
// supervisor, so the contract suite exercises Server and Client end to end.
type remote struct {
	t         *testing.T
	env       *fakeenv.Environment
	checkouts *fakeenv.Checkouts
	farm      *farm
}

// farm is the set of "processes" that outlive one remote.
type farm struct {
	mu   sync.Mutex
	runs map[lifecycle.Ref]*served
}

type served struct {
	sup      *supervisor.Supervisor
	listener *bufconn.Listener
	server   *grpc.Server
	gone     bool
}

func (s *served) kill() {
	s.server.Stop()
	s.sup.Close()
}

func (f *farm) closeAll() {
	f.mu.Lock()
	defer f.mu.Unlock()
	for _, s := range f.runs {
		s.kill()
	}
}

var _ supervisor.Supervisors = (*remote)(nil)

func (r *remote) connect(ctx context.Context, s *served) (supervisor.Handle, error) {
	conn, err := grpc.NewClient("passthrough:///supervisor",
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return s.listener.DialContext(ctx) }),
		grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		return nil, err
	}
	r.t.Cleanup(func() { conn.Close() })
	return rpc.Connect(ctx, conn, "test")
}

func (r *remote) Spawn(ctx context.Context, l lifecycle.Launch) (supervisor.Handle, error) {
	r.farm.mu.Lock()
	defer r.farm.mu.Unlock()
	if _, ok := r.farm.runs[l.Ref()]; ok {
		return nil, &core.Failure{Kind: core.FailureConflict, Message: "already has a run"}
	}
	sup := supervisor.New(supervisor.Config{
		Launch: l, Environments: fakeenv.Environments{core.EnvironmentHost: r.env},
		Checkouts: r.checkouts, Spool: &supervisor.MemorySpool{},
	})
	s := &served{sup: sup, listener: bufconn.Listen(1 << 20), server: grpc.NewServer()}
	cliv1.RegisterSupervisorServiceServer(s.server, rpc.NewServer(sup, "test"))
	go s.server.Serve(s.listener)
	// A released supervisor's process exits: its server stops.
	go func() {
		<-sup.Released()
		s.server.GracefulStop()
	}()
	r.farm.runs[l.Ref()] = s
	return r.connect(ctx, s)
}

func (r *remote) Runs(ctx context.Context) ([]supervisor.Run, error) {
	r.farm.mu.Lock()
	defer r.farm.mu.Unlock()
	var out []supervisor.Run
	for ref, s := range r.farm.runs {
		h, err := r.connect(ctx, s)
		if err != nil || s.sup.Phase() == supervisor.PhaseReleased {
			out = append(out, supervisor.Run{Ref: ref, Presence: lifecycle.PresenceGone})
			continue
		}
		out = append(out, supervisor.Run{Ref: ref, Presence: lifecycle.PresenceAlive, Handle: h})
	}
	return out, nil
}

func (r *remote) Forget(ctx context.Context, ref lifecycle.Ref) error {
	r.farm.mu.Lock()
	s, ok := r.farm.runs[ref]
	r.farm.mu.Unlock()
	if !ok {
		return nil
	}
	// A released supervisor is gone even while its server is still stopping.
	if s.sup.Phase() != supervisor.PhaseReleased {
		if _, err := r.connect(ctx, s); err == nil {
			return &core.Failure{Kind: core.FailureConflict, Message: "the supervisor still answers"}
		}
	}
	r.farm.mu.Lock()
	delete(r.farm.runs, ref)
	r.farm.mu.Unlock()
	return nil
}

func TestRemotePassesTheContract(t *testing.T) {
	suptest.Run(t, func(t *testing.T) suptest.Rig {
		gates := fakeenv.NewMemoryGates()
		f := &farm{runs: map[lifecycle.Ref]*served{}}
		env := fakeenv.New(core.EnvironmentHost, gates)
		checkouts := &fakeenv.Checkouts{}
		t.Cleanup(func() {
			f.closeAll()
			if n := env.Active(); n != 0 {
				t.Errorf("%d commands still run", n)
			}
			if n := checkouts.Open(); n != 0 {
				t.Errorf("%d checkouts still open", n)
			}
		})
		mk := func() supervisor.Supervisors { return &remote{t: t, env: env, checkouts: checkouts, farm: f} }
		return suptest.Rig{
			Supervisors: mk(),
			Restart:     mk,
			Open:        gates.Release,
			Kill: func(ref lifecycle.Ref) {
				f.mu.Lock()
				s := f.runs[ref]
				f.mu.Unlock()
				s.kill()
			},
		}
	})
}

func TestHandshakeRefusesWhatIsNotTheDaemon(t *testing.T) {
	sup := supervisor.New(supervisor.Config{
		Launch: lifecycle.Launch{}, Environments: fakeenv.Environments{},
		Checkouts: &fakeenv.Checkouts{}, Spool: &supervisor.MemorySpool{},
	})
	srv := rpc.NewServer(sup, "test")
	_, err := srv.Handshake(context.Background(), &cliv1.HandshakeRequest{Component: cliv1.Component_COMPONENT_CLI})
	if err == nil {
		t.Fatal("a supervisor answered a handshake from a client")
	}
}

func TestHandshakeJudgesCompatibility(t *testing.T) {
	sup := supervisor.New(supervisor.Config{
		Launch: lifecycle.Launch{}, Environments: fakeenv.Environments{},
		Checkouts: &fakeenv.Checkouts{}, Spool: &supervisor.MemorySpool{},
	})
	srv := rpc.NewServer(sup, "test")
	ours := core.CurrentVersions()
	for _, tc := range []struct {
		name   string
		daemon core.Versions
		want   cliv1.Compatibility
	}{
		{"same build", ours, cliv1.Compatibility_COMPATIBILITY_COMPATIBLE},
		{"a daemon older than the supervisor's minimum", core.Versions{Protocol: ours.MinProtocol - 1, MinProtocol: 0, Schema: ours.Schema}, cliv1.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD},
		{"a daemon that needs a newer supervisor", core.Versions{Protocol: ours.Protocol + 1, MinProtocol: ours.Protocol + 1, Schema: ours.Schema}, cliv1.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD},
	} {
		t.Run(tc.name, func(t *testing.T) {
			res, err := srv.Handshake(context.Background(), &cliv1.HandshakeRequest{
				Component: cliv1.Component_COMPONENT_DAEMON, Versions: wireVersions(tc.daemon),
			})
			if err != nil {
				t.Fatal(err)
			}
			if res.GetCompatibility() != tc.want {
				t.Fatalf("compatibility = %v, want %v", res.GetCompatibility(), tc.want)
			}
		})
	}
}

func wireVersions(v core.Versions) *cliv1.BuildVersions {
	return &cliv1.BuildVersions{ProtocolVersion: v.Protocol, MinProtocolVersion: v.MinProtocol, SchemaVersion: v.Schema}
}
