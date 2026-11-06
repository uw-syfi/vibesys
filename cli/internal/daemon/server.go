package daemon

import (
	"buf.build/go/protovalidate"
	"context"
	"fmt"
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
	app       *app.App
	binary    string
	inherited Inherited
	machine   Machine
	// neighbors lists the other daemons of the machine for the status; nil lists none.
	neighbors func() []Neighbor

	// ctx is cancelled when the daemon is asked to stop. Every call is bound to it, so a call
	// that waits for a job does not hold the daemon's shutdown.
	ctx      context.Context
	shutdown context.CancelFunc
	once     sync.Once
}

var _ cliv1.DaemonServiceServer = (*Server)(nil)

// NewServer returns a server for the App. binaryVersion is the build's release version, which
// the handshake and the status report. inherited is what the jobs inherit, which the status
// reports.
func NewServer(a *app.App, binaryVersion string, inherited Inherited) *Server {
	ctx, cancel := context.WithCancel(context.Background())
	return &Server{app: a, binary: binaryVersion, inherited: inherited, ctx: ctx, shutdown: cancel}
}

// Machine is how a daemon takes this machine for its user, the one daemon of the user on it, or
// records itself as a sandbox daemon beside that one; and how it finds the sandbox daemons, which
// its status lists so that none runs unseen.
type Machine interface {
	// Claim takes the machine, or records the sandbox daemon, until release. It fails when another
	// daemon holds the machine, naming it.
	Claim(ctx context.Context) (release func() error, err error)
	// Sandbox says whether this daemon is a sandbox daemon.
	Sandbox() bool
	// Sandboxes are the sandbox daemons of the user on this machine that run.
	Sandboxes() ([]*cliv1.MachineDaemon, error)
}

// WithMachine has the status report the machine: whether this daemon is a sandbox daemon, and the
// sandbox daemons that run. Nil reports neither.
func (s *Server) WithMachine(m Machine) *Server {
	s.machine = m
	return s
}

// WithNeighbors has the status list the other daemons of the machine as f finds them. A nil f
// lists none.
func (s *Server) WithNeighbors(f func() []Neighbor) *Server {
	s.neighbors = f
	return s
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

// call resolves the context a request carries: the task is parsed and the caller's directory is
// resolved to its repository, once, before the request reaches the app.
func (s *Server) call(ctx context.Context, c *cliv1.CallContext) (core.CallContext, error) {
	return s.app.Resolve(ctx, app.Caller{
		Directory: c.GetRepository(), Task: c.GetTask(), Run: core.RunID(c.GetRunId()),
		Harness: c.GetHarness(), Agent: c.GetAgent(),
	})
}

// Hello implements cliv1.DaemonServiceServer.
func (s *Server) Hello(_ context.Context, req *cliv1.HelloRequest) (*cliv1.HelloResponse, error) {
	switch req.GetComponent() {
	case cliv1.Component_COMPONENT_CLI, cliv1.Component_COMPONENT_ORCHESTRATOR:
	default:
		return nil, wire.Error(&core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"component: %s is not a client of the daemon; it serves the command line tool and the orchestrator", req.GetComponent())})
	}
	ours := core.CurrentVersions()
	compat := core.CheckCompatibility(wire.ToVersions(req.GetVersions()), ours)
	return &cliv1.HelloResponse{Versions: wire.BuildVersions(s.binary, ours), Compatibility: wire.Compatibility(compat)}, nil
}

// WatchTask implements cliv1.DaemonServiceServer. It reports a task's state until the call ends,
// as a reset, then the changes. Protocol 3 ignores the resume token and resets on every
// connection.
func (s *Server) WatchTask(req *cliv1.WatchTaskRequest, stream cliv1.DaemonService_WatchTaskServer) error {
	ctx, cancel := s.bound(stream.Context())
	defer cancel()
	judged, err := wire.JudgedCommits(req)
	if err != nil {
		return wire.Error(err)
	}
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return wire.Error(err)
	}
	epoch := s.app.Epoch()
	var sequence uint64
	err = s.app.WatchTask(ctx, call.Scope, judged, func(send core.Send) error {
		for _, m := range wire.TaskMessages(send) {
			sequence++
			m.Epoch, m.Sequence = epoch, sequence
			if err := protovalidate.Validate(m); err != nil {
				return err
			}
			if err := stream.Send(m); err != nil {
				return err
			}
		}
		return nil
	})
	return wire.Error(err)
}

// Status implements cliv1.DaemonServiceServer.
func (s *Server) Status(context.Context, *cliv1.StatusRequest) (*cliv1.StatusResponse, error) {
	st := s.app.Status()
	out := &cliv1.StatusResponse{
		Versions:          wire.BuildVersions(s.binary, core.CurrentVersions()),
		Pid:               uint32(os.Getpid()),
		StartedAt:         timestamppb.New(st.StartedAt),
		QueuedJobs:        uint32(st.Queued),
		RunningJobs:       uint32(st.Running),
		Environment:       inheritedVariables(s.inherited.Environ),
		CheckoutDirectory: s.inherited.CheckoutDirectory,
		Leases:            heldLeases(st.Leases),
		LeaseConflicts:    leaseConflicts(st.Conflicts),
		OtherDaemons:      s.otherDaemons(),
	}
	if st.Drain != nil {
		out.Drain = wire.DaemonDrain(*st.Drain)
	}
	if s.machine != nil {
		out.Sandbox = s.machine.Sandbox()
		sandboxes, err := s.machine.Sandboxes()
		if err != nil {
			return nil, wire.Error(fmt.Errorf("listing the sandbox daemons: %w", err))
		}
		out.Sandboxes = sandboxes
	}
	return out, nil
}

func (s *Server) otherDaemons() []*cliv1.DaemonProcess {
	if s.neighbors == nil {
		return nil
	}
	var out []*cliv1.DaemonProcess
	for _, n := range s.neighbors() {
		if n.PID > 0 {
			out = append(out, &cliv1.DaemonProcess{Pid: uint32(n.PID), User: n.User})
		}
	}
	return out
}

// Stop implements cliv1.DaemonServiceServer. Jobs that run have supervisors of their own and go
// on; the next daemon finds them.
func (s *Server) Stop(context.Context, *cliv1.StopRequest) (*cliv1.StopResponse, error) {
	s.Shutdown()
	return &cliv1.StopResponse{}, nil
}

// ForgetRepository implements cliv1.DaemonServiceServer.
func (s *Server) ForgetRepository(ctx context.Context, req *cliv1.ForgetRepositoryRequest) (*cliv1.ForgetRepositoryResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	out, err := s.app.ForgetRepository(ctx, req.GetRepository())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ForgetRepositoryResponse{Tasks: out.Tasks, Left: out.Left}, nil
}

// Submit implements cliv1.DaemonServiceServer.
func (s *Server) Submit(ctx context.Context, req *cliv1.SubmitRequest) (*cliv1.SubmitResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	spec, err := wire.ToJobSpec("spec", req.GetSpec())
	if err != nil {
		return nil, wire.Error(err)
	}
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	out, err := s.app.Submit(ctx, app.SubmitRequest{
		Context: call,
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
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return wire.Error(err)
	}
	err = s.app.Watch(ctx, call.Scope, ids, func(j core.Job) error {
		return stream.Send(&cliv1.WatchResponse{Job: wire.Job(j)})
	})
	return wire.Error(err)
}

// Wait implements cliv1.DaemonServiceServer. The stream ends with the outcome the app decided.
func (s *Server) Wait(req *cliv1.WaitRequest, stream cliv1.DaemonService_WaitServer) error {
	ctx, cancel := s.bound(stream.Context())
	defer cancel()
	wait, err := wire.ToWaitFor(req)
	if err != nil {
		return wire.Error(err)
	}
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return wire.Error(err)
	}
	outcome, err := s.app.Wait(ctx, call.Scope, wait, func(j core.Job) error {
		return stream.Send(&cliv1.WaitResponse{Event: &cliv1.WaitResponse_Job{Job: wire.Job(j)}})
	})
	if err != nil {
		return wire.Error(err)
	}
	return wire.Error(stream.Send(&cliv1.WaitResponse{Event: &cliv1.WaitResponse_Outcome{Outcome: wire.WaitOutcome(outcome)}}))
}

// Cancel implements cliv1.DaemonServiceServer.
func (s *Server) Cancel(ctx context.Context, req *cliv1.CancelRequest) (*cliv1.CancelResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	job, err := s.app.Cancel(ctx, call, core.JobID(req.GetJobId()), req.GetReason())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.CancelResponse{Job: wire.Job(job)}, nil
}

// AddNote implements cliv1.DaemonServiceServer.
func (s *Server) AddNote(ctx context.Context, req *cliv1.AddNoteRequest) (*cliv1.AddNoteResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	job, err := s.app.AddNote(ctx, call, core.JobID(req.GetJobId()), req.GetText())
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
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	jobs, err := s.app.Jobs(ctx, call.Scope, filter)
	if err != nil {
		return nil, wire.Error(err)
	}
	var queued []core.JobID
	for _, j := range jobs {
		if j.State == core.JobQueued {
			queued = append(queued, j.ID)
		}
	}
	return &cliv1.ListJobsResponse{Jobs: wire.Jobs(jobs), Waits: wire.JobWaits(jobs, s.app.Waits(call.Scope, queued))}, nil
}

// ReadLogs implements cliv1.DaemonServiceServer.
func (s *Server) ReadLogs(req *cliv1.ReadLogsRequest, stream cliv1.DaemonService_ReadLogsServer) error {
	ctx, cancel := s.bound(stream.Context())
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return wire.Error(err)
	}
	err = s.app.ReadLogs(ctx, call.Scope, core.JobID(req.GetJobId()), req.GetOffset(), req.GetFollow(), func(c core.LogChunk) error {
		return stream.Send(wire.LogChunk(c))
	})
	return wire.Error(err)
}

// Lab implements cliv1.DaemonServiceServer. A call that streams, such as an exec in the
// foreground, ends with the daemon: a command it started is ended when its caller is gone.
func (s *Server) Lab(req *cliv1.LabRequest, stream cliv1.DaemonService_LabServer) error {
	ctx, cancel := s.bound(stream.Context())
	defer cancel()
	if err := protovalidate.Validate(req); err != nil {
		return wire.Error(&core.Failure{Kind: core.FailureInvalidInput, Message: err.Error()})
	}
	labCall, err := wire.ToLabCall("call", req.GetCall())
	if err != nil {
		return wire.Error(err)
	}
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return wire.Error(err)
	}
	err = s.app.Lab(ctx, call, core.JobID(req.GetJobId()), labCall, func(r core.LabReply) error {
		return stream.Send(&cliv1.LabResponse{Reply: wire.LabReply(r)})
	})
	return wire.Error(err)
}

// heldLeases returns the contract's messages for the leases a daemon's jobs hold.
func heldLeases(ls []app.HeldLease) []*cliv1.HeldLease {
	var out []*cliv1.HeldLease
	for _, l := range ls {
		m := &cliv1.HeldLease{JobId: string(l.Job), Task: string(l.Task), Resource: string(l.Resource), Key: string(l.Key), Where: l.Where}
		if !l.Since.IsZero() {
			m.Since = timestamppb.New(l.Since)
		}
		out = append(out, m)
	}
	return out
}

// leaseConflicts returns the contract's messages for the leases jobs that run could not take.
func leaseConflicts(cs []app.LeaseConflict) []*cliv1.LeaseConflict {
	var out []*cliv1.LeaseConflict
	for _, c := range cs {
		m := &cliv1.LeaseConflict{JobId: string(c.Job), Resource: string(c.Resource), Detail: c.Detail}
		if c.Holder != (core.LeaseHolder{}) {
			m.Holder = wire.LeaseHolder(c.Holder)
		}
		out = append(out, m)
	}
	return out
}
