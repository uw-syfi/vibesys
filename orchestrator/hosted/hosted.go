// Package hosted is the orchestrator as a host process serves it: vibesys-host opens it beside its own
// API, registers its operator service on its own gRPC server and socket, and keeps itself up while
// a run is live. It is the one package outside this module's internals that other modules import.
//
// Exactly one process holds the orchestrator's state directory, through the store's lock. Start
// never fails on one that cannot take it, or whose state directory is refused: it says so as a typed
// Unserved, naming the holder's release when there is one. The host decides what to do about that.
// One that stays up answers UNAVAILABLE with the reason to every orchestrator call, and
// HostControlService, which is served either way, reports the Unserved.
package hosted

import (
	"context"
	"errors"
	"log/slog"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/orchestrator/gatewayroot"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
	"github.com/uw-syfi/vibesys/orchestrator/hostlock"
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
	// Root is the host's root, which its default state directory follows from.
	Root string
	// Config is the host's configuration, as it read it when it started.
	Config *pb.HostConfig
	// Getenv reads the XDG places of the host's own environment, which is only ever what
	// hostconfig.Environ made it. Nothing that configures the host comes from it.
	Getenv func(string) string
	Home   string
	// APISchemaHash is what the host's HostService answers with, for HostControlService to report.
	APISchemaHash string
	// Stop ends the host process. HostControlService calls it, after the reply, when asked to stop.
	Stop func()
	// BuildID is which program a host of no release is, and empty for one of a release.
	BuildID string
	// Connected counts the host's open connections, the asker's among them. Nil counts none.
	Connected func() uint32
}

// Orchestrator is one opened orchestrator, or the reason there is none in this process.
type Orchestrator struct {
	d      *production.Daemon
	reason string
	// why says why d is nil, in the form HostControlService reports it, as it was when the host
	// started. state is the directory it found held, for looking again at who holds it.
	why     *pb.Unserved
	state   string
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
	parts := production.Parts{Config: o.Config, Find: release.FinderFor(o.Executable), Files: production.OSFiles{}, Home: o.Home, Runner: production.ExecRunner{}}
	x.control = NewHostControl(ControlConfig{Release: o.Version, Started: production.SystemClock{}.Now(), Parts: parts, Host: x, Stop: o.Stop, ConfigDigest: hostconfig.Digest(o.Config), APISchemaHash: o.APISchemaHash, BuildID: o.BuildID, Connected: o.Connected})
	layout, err := production.LayoutFor(o.Config.GetStateDirectory(), gatewayroot.StateDirectory(o.Root, o.Home, o.Getenv))
	if err != nil {
		x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, "", "the orchestrator's state directory is refused: "+err.Error())
		x.why.StateRefusal = production.StateRefusalOf(err)
		return x
	}
	sidecars, err := production.SidecarsFor(parts, layout.State.Sessions(), o.Executable, o.Log)
	if err != nil {
		return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, "", err.Error())
	}
	sidecars.Place = layout.State.Place
	searches := production.SearchFor()
	catalog := &production.SidecarCatalog{Parts: parts, Runner: production.ExecRunner{}, Environ: hostconfig.Environ(o.Config, o.Home, o.Getenv)}
	wiring := production.Wiring{Sidecars: sidecars, Search: searches, Jobs: search.Source{Store: searches.Store}, Tree: production.OSTree{}, Version: o.Version, JobTool: parts.JobToolEndpoint, Parts: parts, Root: o.Root, Models: production.HostModels{Home: o.Home, Catalog: catalog, Log: o.Log}, SetupReads: production.NewCLISetupReads()}
	d, err := production.AwaitHandOver(ctx, o.Root, time.After, func() (*production.Daemon, error) { return production.Open(layout, wiring, o.Log) })
	if err != nil {
		var held *production.HeldError
		if errors.As(err, &held) {
			x.state = layout.State.Path()
			return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, held.Holder.GetRelease(), "the orchestrator is not served by this process: "+err.Error())
		}
		return x.unavailable(o.Log, pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED, "", "the orchestrator is not served by this process: "+err.Error())
	}
	x.d = d
	// The models each harness runs are asked of the sidecar program once, now, so that the first
	// start does not wait for it. Nothing depends on the answer being there.
	go func() { _, _ = catalog.Models(ctx) }()
	go func() {
		err := d.Run(ctx)
		// Done means the state directory's lock is free: a host releases its root's lock after this,
		// so whoever gets that lock next finds the state free too.
		d.Close()
		x.done <- err
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

// Unserved implements Source. What was held when the host started may not be held now, so that one
// answer is looked at again each time it is asked for: a holder that has gone is its own reason, and a
// new holder is named in place of the old one. Nothing else about why a host does not serve changes
// while it runs.
func (x *Orchestrator) Unserved() *pb.Unserved {
	if x.why.GetReason() != pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE {
		return x.why
	}
	holder, held, err := hostlock.StateHolder(x.state)
	switch {
	case err != nil:
		return x.why
	case !held:
		return &pb.Unserved{
			Reason: pb.UnservedReason_UNSERVED_REASON_HOLDER_GONE, HeldByRelease: x.why.GetHeldByRelease(),
			Detail: "the process that held the orchestrator's state in " + x.state + " has gone, and this host, started while it held it, does not take it over",
		}
	case holder != nil:
		return &pb.Unserved{
			Reason: pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, HeldByRelease: holder.GetRelease(),
			Detail: "the orchestrator is not served by this process: " + (&production.HeldError{Dir: x.state, Holder: holder}).Error(),
		}
	}
	return x.why
}

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
