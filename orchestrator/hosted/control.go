package hosted

import (
	"context"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
)

// Source is what HostControlService reads of the host process.
type Source interface {
	// LiveRuns counts the runs that have not reached their end.
	LiveRuns() uint32
	// Unserved says why this process does not serve the orchestrator, or is nil when it does.
	Unserved() *pb.Unserved
}

// PartsReport says which parts a run is started with can be used now.
type PartsReport interface {
	Readiness() []*pb.PartReadiness
}

// ControlConfig is what HostControlService is made of.
type ControlConfig struct {
	// Release is the host's release version, or "dev" for a build of no release.
	Release string
	Started time.Time
	Parts   PartsReport
	Host    Source
	// Stop ends the host process. It is called once, after the reply to the call that asked.
	Stop func()
}

// NewHostControl is the host's HostControlService. It is registered whether or not the orchestrator
// is served, so any client can ask any host what it is and stop it.
func NewHostControl(c ControlConfig) pb.HostControlServiceServer { return &control{c: c} }

type control struct {
	pb.UnimplementedHostControlServiceServer
	c ControlConfig
}

// Identify implements pb.HostControlServiceServer. The parts are resolved now, not when the host
// started, so the answer follows a repaired install.
func (c *control) Identify(_ context.Context, req *pb.IdentifyRequest) (*pb.HostIdentity, error) {
	id := &pb.HostIdentity{
		Release:   c.c.Release,
		Relation:  release.Relate(c.c.Release, req.GetClientRelease()),
		StartedAt: timestamppb.New(c.c.Started),
		LiveRuns:  c.c.Host.LiveRuns(),
		Parts:     c.c.Parts.Readiness(),
	}
	if unserved := c.c.Host.Unserved(); unserved != nil {
		id.Orchestrator = &pb.HostIdentity_Unserved{Unserved: unserved}
	} else {
		id.Orchestrator = &pb.HostIdentity_Serving{Serving: &pb.Serving{}}
	}
	return id, nil
}

// Stop implements pb.HostControlServiceServer. The host ends after this reply has been written:
// the call's context ends then, and not before. A host that serves live runs and was asked to stop
// only when idle refuses. A run that starts between that check and the stop loses its host like any
// run whose host crashed, and the next host recovers it.
func (c *control) Stop(ctx context.Context, req *pb.StopHostRequest) (*pb.StopHostResponse, error) {
	if live := c.c.Host.LiveRuns(); req.GetOnlyIfIdle() && live > 0 {
		return &pb.StopHostResponse{Outcome: &pb.StopHostResponse_Refused{Refused: &pb.Busy{LiveRuns: live}}}, nil
	}
	go func() {
		<-ctx.Done()
		c.c.Stop()
	}()
	return &pb.StopHostResponse{Outcome: &pb.StopHostResponse_Stopping{Stopping: &pb.Stopping{}}}, nil
}
