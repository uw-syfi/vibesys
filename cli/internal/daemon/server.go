package daemon

import (
	"context"
	"os"
	"sync"

	"google.golang.org/protobuf/types/known/timestamppb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Server serves an App as the DaemonService. Create it with NewServer.
type Server struct {
	cliv1.UnimplementedDaemonServiceServer
	app    *app.App
	binary string

	// ctx is cancelled when the daemon is asked to stop. Every call is bound to it, so a call
	// that waits for a job does not hold the daemon's shutdown.
	ctx      context.Context
	shutdown context.CancelFunc
	once     sync.Once
}

var _ cliv1.DaemonServiceServer = (*Server)(nil)

// NewServer returns a server for the App. binaryVersion is the build's release version, which
// the handshake and the status report.
func NewServer(a *app.App, binaryVersion string) *Server {
	ctx, cancel := context.WithCancel(context.Background())
	return &Server{app: a, binary: binaryVersion, ctx: ctx, shutdown: cancel}
}

// Stopping is closed when the daemon has been asked to stop, by the Stop call or by Shutdown.
func (s *Server) Stopping() <-chan struct{} { return s.ctx.Done() }

// Shutdown asks the daemon to stop. It returns at once; the process notices through Stopping.
func (s *Server) Shutdown() { s.once.Do(s.shutdown) }

// bound returns a context that ends when the call's does or when the daemon is stopping.
func (s *Server) bound(ctx context.Context) (context.Context, context.CancelFunc) {
	ctx, cancel := context.WithCancel(ctx)
	detach := context.AfterFunc(s.ctx, cancel)
	return ctx, func() { detach(); cancel() }
}

func scopeOf(c *cliv1.CallContext) app.Scope {
	ctx := wire.ToCallContext(c)
	return app.Scope{Repository: ctx.Repository, Task: ctx.Task}
}

// Hello implements cliv1.DaemonServiceServer.
func (s *Server) Hello(_ context.Context, req *cliv1.HelloRequest) (*cliv1.HelloResponse, error) {
	if req.GetComponent() != cliv1.Component_COMPONENT_CLI {
		return nil, wire.Error(&core.Failure{Kind: core.FailureInvalidInput, Message: "component: the daemon serves the command line tool only"})
	}
	ours := core.CurrentVersions()
	compat := core.CheckCompatibility(wire.ToVersions(req.GetVersions()), ours)
	return &cliv1.HelloResponse{Versions: wire.BuildVersions(s.binary, ours), Compatibility: wire.Compatibility(compat)}, nil
}

// Status implements cliv1.DaemonServiceServer.
func (s *Server) Status(context.Context, *cliv1.StatusRequest) (*cliv1.StatusResponse, error) {
	st := s.app.Status()
	return &cliv1.StatusResponse{
		Versions:    wire.BuildVersions(s.binary, core.CurrentVersions()),
		Pid:         uint32(os.Getpid()),
		StartedAt:   timestamppb.New(st.StartedAt),
		QueuedJobs:  uint32(st.Queued),
		RunningJobs: uint32(st.Running),
	}, nil
}

// Stop implements cliv1.DaemonServiceServer. Jobs that run have supervisors of their own and go
// on; the next daemon finds them.
func (s *Server) Stop(context.Context, *cliv1.StopRequest) (*cliv1.StopResponse, error) {
	s.Shutdown()
	return &cliv1.StopResponse{}, nil
}

// Submit implements cliv1.DaemonServiceServer.
func (s *Server) Submit(ctx context.Context, req *cliv1.SubmitRequest) (*cliv1.SubmitResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	spec, err := wire.ToJobSpec("spec", req.GetSpec())
	if err != nil {
		return nil, wire.Error(err)
	}
	out, err := s.app.Submit(ctx, app.SubmitRequest{
		Context: wire.ToCallContext(req.GetContext()),
		Spec:    spec,
		NoCache: req.GetNoCache(),
		Note:    req.GetNote(),
		Plan:    core.PlanNumber(req.GetPlan()),
	})
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.SubmitResponse{Job: wire.Job(out.Job), Disposition: wire.SubmitDisposition(out.Disposition)}, nil
}

// Watch implements cliv1.DaemonServiceServer.
func (s *Server) Watch(req *cliv1.WatchRequest, stream cliv1.DaemonService_WatchServer) error {
	ctx, cancel := s.bound(stream.Context())
	defer cancel()
	ids := make([]core.JobID, 0, len(req.GetJobIds()))
	for _, id := range req.GetJobIds() {
		ids = append(ids, core.JobID(id))
	}
	err := s.app.Watch(ctx, scopeOf(req.GetContext()), ids, func(j core.Job) error {
		return stream.Send(&cliv1.WatchResponse{Job: wire.Job(j)})
	})
	return wire.Error(err)
}

// Cancel implements cliv1.DaemonServiceServer.
func (s *Server) Cancel(ctx context.Context, req *cliv1.CancelRequest) (*cliv1.CancelResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	job, err := s.app.Cancel(ctx, wire.ToCallContext(req.GetContext()), core.JobID(req.GetJobId()), req.GetReason())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.CancelResponse{Job: wire.Job(job)}, nil
}

// AddNote implements cliv1.DaemonServiceServer.
func (s *Server) AddNote(ctx context.Context, req *cliv1.AddNoteRequest) (*cliv1.AddNoteResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	job, err := s.app.AddNote(ctx, wire.ToCallContext(req.GetContext()), core.JobID(req.GetJobId()), req.GetText())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.AddNoteResponse{Job: wire.Job(job)}, nil
}

// ListJobs implements cliv1.DaemonServiceServer.
func (s *Server) ListJobs(ctx context.Context, req *cliv1.ListJobsRequest) (*cliv1.ListJobsResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	filter, err := wire.ToJobFilter(req)
	if err != nil {
		return nil, wire.Error(err)
	}
	jobs, err := s.app.Jobs(ctx, scopeOf(req.GetContext()), filter)
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ListJobsResponse{Jobs: wire.Jobs(jobs)}, nil
}

// ReadLogs implements cliv1.DaemonServiceServer.
func (s *Server) ReadLogs(req *cliv1.ReadLogsRequest, stream cliv1.DaemonService_ReadLogsServer) error {
	ctx, cancel := s.bound(stream.Context())
	defer cancel()
	err := s.app.ReadLogs(ctx, scopeOf(req.GetContext()), core.JobID(req.GetJobId()), req.GetOffset(), req.GetFollow(), func(c core.LogChunk) error {
		return stream.Send(wire.LogChunk(c))
	})
	return wire.Error(err)
}
