// Package hosted is the orchestrator as a host process serves it: vibesys-host opens it beside its own
// API, registers its operator service on its own gRPC server and socket, and keeps itself up while
// a run is live. It is the one package outside this module's internals that other modules import.
//
// Exactly one process holds the orchestrator's state directory, through the store's lock. A process
// that cannot take it, or whose state directory is refused, still starts: every orchestrator call
// it receives answers UNAVAILABLE with the reason, and HostControlService, which is served either
// way, says so as a typed Unserved, naming the holder's release when there is one.
package hosted

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"path/filepath"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/production"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// Options are what the host process tells the orchestrator about itself.
type Options struct {
	// Version is the host binary's version, reported by Hello and named to a second process.
	Version string
	// Executable is the host binary. Each agent's tool server runs it as `Executable mcp`.
	Executable string
	Log        *slog.Logger
	// Getenv and Home resolve the state directory and the sidecar setting.
	Getenv func(string) string
	Home   string
	// Stop ends the host process. HostControlService calls it, after the reply, when asked to stop.
	Stop func()
}

// Orchestrator is one opened orchestrator, or the reason there is none in this process.
type Orchestrator struct {
	d      *production.Daemon
	reason string
	// why says why d is nil, in the form HostControlService reports it.
	why     *pb.Unserved
	control pb.HostControlServiceServer
	live    chan bool
	done    chan error
}

// Start opens the orchestrator and runs it until ctx ends. It never fails: a process that cannot
// hold the orchestrator serves UNAVAILABLE and says why.
func Start(ctx context.Context, o Options) *Orchestrator {
	x := &Orchestrator{live: make(chan bool, 1), done: make(chan error, 1)}
	// The parts are resolved when a run is started and when a session is launched, and when
	// HostControlService is asked, never here: a part that is missing now is not missing for good.
	parts := production.Parts{Getenv: o.Getenv, Find: release.FinderFor(o.Executable), Files: production.OSFiles{}, Home: o.Home}
	x.control = NewHostControl(ControlConfig{Release: o.Version, Started: production.SystemClock{}.Now(), Parts: parts, Host: x, Stop: o.Stop})
	layout, err := production.LayoutFor(o.Getenv, o.Home)
	if err != nil {
		return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, "", "the orchestrator's state directory is refused: "+err.Error())
	}
	sidecars, err := production.SidecarsFor(parts, filepath.Join(layout.State.Path(), "sessions"), o.Executable, o.Log)
	if err != nil {
		return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, "", err.Error())
	}
	searches := production.SearchFor()
	d, err := production.Open(layout, production.Wiring{Sidecars: sidecars, Search: searches, Jobs: search.Source{Store: searches.Store}, Tree: production.OSTree{}, Version: o.Version, Locate: parts.Location, Parts: parts}, o.Log)
	if err != nil {
		var held *production.HeldError
		if errors.As(err, &held) {
			return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, held.Holder.GetRelease(), "the orchestrator is not served by this process: "+err.Error())
		}
		return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, "", "the orchestrator is not served by this process: "+err.Error())
	}
	x.d = d
	go func() {
		defer d.Close()
		x.done <- d.Run(ctx)
	}()
	go x.watchLive(ctx)
	return x
}

func (x *Orchestrator) unavailable(log *slog.Logger, kind pb.UnservedReason, heldBy, reason string) *Orchestrator {
	log.Warn("orchestrator unavailable", "reason", reason)
	x.reason = reason
	x.why = &pb.Unserved{Reason: kind, HeldByRelease: heldBy, Detail: reason}
	close(x.done)
	return x
}

// LiveRuns implements Source: the runs that have not reached their end, none when the orchestrator
// is not served here.
func (x *Orchestrator) LiveRuns() uint32 {
	if x.d == nil {
		return 0
	}
	return x.d.LiveRuns()
}

// Unserved implements Source.
func (x *Orchestrator) Unserved() *pb.Unserved { return x.why }

// Reason is why this process does not serve the orchestrator, or empty when it does.
func (x *Orchestrator) Reason() string { return x.reason }

// Done ends when the orchestrator stops; a value is the error it stopped with. A process that
// serves no orchestrator has it closed from the start.
func (x *Orchestrator) Done() <-chan error { return x.done }

// Live sends true when the first run starts and false when the last live run ends. While it is
// true the host must not exit for being idle.
func (x *Orchestrator) Live() <-chan bool { return x.live }

func (x *Orchestrator) watchLive(ctx context.Context) {
	last := false
	for {
		now := x.d.Live()
		if now != last {
			select {
			case x.live <- now:
				last = now
			case <-ctx.Done():
				return
			}
		}
		select {
		case <-x.d.Commits():
		case <-ctx.Done():
			return
		}
	}
}

// Register adds HostControlService, always, and the operator's OrchestratorService to a host's gRPC
// server. Calls that reach the latter carry no agent credentials; agents use the agent-only socket
// the orchestrator serves itself.
func (x *Orchestrator) Register(server grpc.ServiceRegistrar) error {
	pb.RegisterHostControlServiceServer(server, x.control)
	if x.d == nil {
		registerUnavailable(server, x.reason)
		return nil
	}
	service, err := x.d.Service(daemon.AudienceOperator)
	if err != nil {
		return err
	}
	service.Register(server)
	return nil
}

// registerUnavailable registers a service whose every method answers UNAVAILABLE with reason.
func registerUnavailable(server grpc.ServiceRegistrar, reason string) {
	err := status.Error(codes.Unavailable, reason)
	desc := pb.OrchestratorService_ServiceDesc
	desc.Methods = append([]grpc.MethodDesc(nil), desc.Methods...)
	for i := range desc.Methods {
		desc.Methods[i].Handler = func(any, context.Context, func(any) error, grpc.UnaryServerInterceptor) (any, error) {
			return nil, err
		}
	}
	desc.Streams = append([]grpc.StreamDesc(nil), desc.Streams...)
	for i := range desc.Streams {
		desc.Streams[i].Handler = func(any, grpc.ServerStream) error { return err }
	}
	server.RegisterService(&desc, pb.UnimplementedOrchestratorServiceServer{})
}

// Environ is the process environment, for Options.Getenv.
func Environ() (func(string) string, string) {
	home, _ := os.UserHomeDir()
	return os.Getenv, home
}
