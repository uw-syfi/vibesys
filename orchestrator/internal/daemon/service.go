package daemon

import (
	"context"
	"errors"
	"fmt"
	"strconv"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// The metadata an agent's tools put on every call. Together they say which session is calling; the
// daemon checks the token and never reads an agent id from a request body.
const (
	MetaAgent   = "vibesys-agent"
	MetaSession = "vibesys-session"
	MetaToken   = "vibesys-token"
)

// Service is OrchestratorService over an Engine and the hub. An operator connects with no
// credentials, over the daemon's private socket. An agent calls with the metadata above, and may
// then ask for its own commands and for the snapshot of its run: nothing else.
type Service struct {
	pb.UnimplementedOrchestratorServiceServer
	e         *Engine
	plans     PlanReader
	authority Authority
	binary    string
}

// NewService returns the service. binary is the release version, for display.
func NewService(e *Engine, plans PlanReader, authority Authority, binary string) *Service {
	return &Service{e: e, plans: plans, authority: authority, binary: binary}
}

// Register adds the service to a gRPC server.
func (s *Service) Register(g grpc.ServiceRegistrar) { pb.RegisterOrchestratorServiceServer(g, s) }

func (s *Service) versions() *pb.BuildVersions {
	return &pb.BuildVersions{BinaryVersion: s.binary, ProtocolVersion: ProtocolVersion, MinProtocolVersion: MinProtocolVersion}
}

// Hello answers a handshake with the daemon's versions and whether the two can talk.
func (s *Service) Hello(_ context.Context, req *pb.HelloRequest) (*pb.HelloResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	c := req.GetClient()
	compat := pb.Compatibility_COMPATIBILITY_COMPATIBLE
	switch {
	case c.GetProtocolVersion() < MinProtocolVersion:
		compat = pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD
	case c.GetMinProtocolVersion() > ProtocolVersion:
		compat = pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD
	}
	return &pb.HelloResponse{Daemon: s.versions(), Compatibility: compat}, nil
}

// caller says who is calling, from the call's metadata.
func (s *Service) caller(ctx context.Context) (core.Caller, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	agent := md.Get(MetaAgent)
	if len(agent) == 0 {
		return core.Operator{}, nil
	}
	n, err := strconv.ParseUint(first(md.Get(MetaSession)), 10, 32)
	session := core.Session{Agent: core.AgentID(agent[0]), N: uint32(n)}
	if err != nil || !s.authority.Valid(session, first(md.Get(MetaToken))) {
		return nil, status.Error(codes.Unauthenticated, "the session token is missing or wrong: start the tool from the environment the orchestrator launched the agent with")
	}
	if !isLive(s.e.State(), session) {
		return nil, status.Errorf(codes.PermissionDenied, "session %d of agent %s is over: the agent was stopped or resumed", n, session.Agent)
	}
	return core.AgentCaller{Agent: session.Agent}, nil
}

func first(v []string) string {
	if len(v) == 0 {
		return ""
	}
	return v[0]
}

// operatorOnly refuses an agent, with a message that says which call it may make.
func operatorOnly(c core.Caller, call string) error {
	if _, agent := c.(core.AgentCaller); agent {
		return status.Errorf(codes.PermissionDenied, "%s is for the operator: an agent may use Dispatch for its own tools and WatchRuns to list its run", call)
	}
	return nil
}

// Dispatch carries out a command as the caller.
func (s *Service) Dispatch(ctx context.Context, req *pb.DispatchRequest) (*pb.DispatchResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	cmd, err := wire.ParseCommandMessage(req.GetCommand(), caller)
	if err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	answer, err := s.e.Command(cmd)
	switch {
	case errors.Is(err, ErrCommandConflict):
		return nil, status.Error(codes.AlreadyExists, err.Error()+": use a new id for a different command")
	case err != nil:
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	return &pb.DispatchResponse{Receipt: wire.ReceiptMessage(answer.Receipt, answer.Sequence)}, nil
}

// runOf is the run an agent caller belongs to.
func (s *Service) runOf(c core.Caller) core.RunID {
	if a, ok := c.(core.AgentCaller); ok {
		agent, _ := s.e.State().Agent(a.Agent)
		return agent.Run
	}
	return ""
}

// WatchRunsFrom opens the stream behind WatchRuns, for callers that poll it.
func (s *Service) WatchRunsFrom(caller core.Caller, after uint64) (*RunsWatch, []*pb.WatchRunsResponse, error) {
	run := s.runOf(caller)
	_, scoped := caller.(core.AgentCaller)
	w := &RunsWatch{
		e: s.e, run: run,
		snapshot: func(state core.State, last uint64) (*pb.WatchRunsResponse, error) {
			snap := &pb.RunsSnapshot{Sequence: last}
			for _, r := range state.Runs() {
				if scoped && r.ID != run {
					continue
				}
				snap.Runs = append(snap.Runs, wire.RunMessage(r))
				for _, a := range state.Agents(r.ID) {
					snap.Agents = append(snap.Agents, wire.AgentMessage(a))
				}
			}
			return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Snapshot{Snapshot: snap}}, nil
		},
		event: func(ev *pb.Event) (*pb.WatchRunsResponse, bool) {
			if scoped && core.RunID(ev.GetRunId()) != run {
				return nil, false
			}
			return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Event{Event: ev}}, true
		},
		delta: func(*pb.Delta) (*pb.WatchRunsResponse, bool) { return nil, false },
	}
	first, err := w.open(hub.Interest{Everything: true}, after)
	return w, first, err
}

// WatchRuns streams a snapshot of runs and agents, then every event after it.
func (s *Service) WatchRuns(req *pb.WatchRunsRequest, stream grpc.ServerStreamingServer[pb.WatchRunsResponse]) error {
	caller, err := s.caller(stream.Context())
	if err != nil {
		return err
	}
	w, first, err := s.WatchRunsFrom(caller, req.GetAfterSequence())
	if err != nil {
		return watchError(err)
	}
	defer w.Close()
	return serve(stream.Context(), w, first, stream.Send)
}

// WatchAgentFrom opens the stream behind WatchAgent, for callers that poll it.
func (s *Service) WatchAgentFrom(agent core.AgentID, pageSize uint32, after uint64) (*AgentWatch, []*pb.WatchAgentResponse, error) {
	a, ok := s.e.State().Agent(agent)
	if !ok {
		return nil, nil, status.Errorf(codes.NotFound, "no agent %s: list the agents of a run with WatchRuns", agent)
	}
	w := &AgentWatch{
		e: s.e, run: a.Run,
		snapshot: func(_ core.State, last uint64) (*pb.WatchAgentResponse, error) {
			page, err := s.itemsPage(agent, 0, int(pageSize))
			if err != nil {
				return nil, err
			}
			page.Sequence = last
			return &pb.WatchAgentResponse{Body: &pb.WatchAgentResponse_Page{Page: page}}, nil
		},
		event: func(ev *pb.Event) (*pb.WatchAgentResponse, bool) {
			switch b := ev.GetBody().(type) {
			case *pb.Event_ItemUpdated:
				if b.ItemUpdated.GetAgentId() == string(agent) {
					return &pb.WatchAgentResponse{Body: &pb.WatchAgentResponse_Item{Item: b.ItemUpdated}}, true
				}
			case *pb.Event_TurnUpdated:
				if b.TurnUpdated.GetAgentId() == string(agent) {
					return &pb.WatchAgentResponse{Body: &pb.WatchAgentResponse_Turn{Turn: b.TurnUpdated}}, true
				}
			}
			return nil, false
		},
		delta: func(d *pb.Delta) (*pb.WatchAgentResponse, bool) {
			return &pb.WatchAgentResponse{Body: &pb.WatchAgentResponse_Delta{Delta: d}}, true
		},
	}
	first, err := w.open(hub.Interest{Agent: agent}, after)
	return w, first, err
}

// WatchAgent streams a page of one agent's newest items, then its items, turns and deltas.
func (s *Service) WatchAgent(req *pb.WatchAgentRequest, stream grpc.ServerStreamingServer[pb.WatchAgentResponse]) error {
	caller, err := s.caller(stream.Context())
	if err != nil {
		return err
	}
	if err := wire.Validate(req); err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	if err := operatorOnly(caller, "WatchAgent"); err != nil {
		return err
	}
	w, first, err := s.WatchAgentFrom(core.AgentID(req.GetAgentId()), req.GetPageSize(), req.GetAfterSequence())
	if err != nil {
		return watchError(err)
	}
	defer w.Close()
	return serve(stream.Context(), w, first, stream.Send)
}

// serve sends the first responses, then whatever the watch has ready each time it says so.
func serve[R any](ctx context.Context, w *watch[R], first []R, send func(R) error) error {
	for _, r := range first {
		if err := send(r); err != nil {
			return err
		}
	}
	for {
		ready, err := w.Poll()
		if err != nil {
			return watchError(err)
		}
		for _, r := range ready {
			if err := send(r); err != nil {
				return err
			}
		}
		if len(ready) > 0 {
			continue
		}
		select {
		case <-ctx.Done():
			return ctx.Err()
		case <-w.Ready():
		}
	}
}

func watchError(err error) error {
	if _, ok := status.FromError(err); ok && status.Code(err) != codes.Unknown {
		return err
	}
	if errors.Is(err, ErrAheadOfLog) {
		return status.Error(codes.OutOfRange, err.Error())
	}
	return status.Error(codes.Unavailable, err.Error())
}

// itemsPage reads a page of an agent's items. A zero before reads the newest.
func (s *Service) itemsPage(agent core.AgentID, before uint32, size int) (*pb.ItemsPage, error) {
	page, err := s.e.Store.Items(agent, before, min(size, store.MaxPageItems))
	if err != nil {
		return nil, fmt.Errorf("read items of %s: %w", agent, err)
	}
	out := &pb.ItemsPage{}
	for _, it := range page.Items {
		out.Items = append(out.Items, wire.ItemMessage(it))
	}
	if page.HasOlder && len(page.Items) > 0 {
		out.NextBeforeOrdinal = page.Items[0].Ordinal
	}
	return out, nil
}

// ListItems reads older history, a page at a time.
func (s *Service) ListItems(ctx context.Context, req *pb.ListItemsRequest) (*pb.ListItemsResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "ListItems"); err != nil {
		return nil, err
	}
	agent := core.AgentID(req.GetAgentId())
	if _, ok := s.e.State().Agent(agent); !ok {
		return nil, status.Errorf(codes.NotFound, "no agent %s: list the agents of a run with WatchRuns", agent)
	}
	var page *pb.ItemsPage
	if err := s.e.Cut(func(_ core.State, last uint64) error {
		var err error
		if page, err = s.itemsPage(agent, req.GetBeforeOrdinal(), int(req.GetPageSize())); err == nil {
			page.Sequence = last
		}
		return err
	}); err != nil {
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	return &pb.ListItemsResponse{Page: page}, nil
}

func (s *Service) planRun(ctx context.Context, runID string, call string) (core.Run, error) {
	caller, err := s.caller(ctx)
	if err != nil {
		return core.Run{}, err
	}
	if err := operatorOnly(caller, call); err != nil {
		return core.Run{}, err
	}
	run, ok := s.e.State().Run(core.RunID(runID))
	if !ok {
		return core.Run{}, status.Errorf(codes.NotFound, "no run %s: list runs with WatchRuns", runID)
	}
	return run, nil
}

// ListPlans reads a run's plans through vibesys-cli.
func (s *Service) ListPlans(ctx context.Context, req *pb.ListPlansRequest) (*pb.ListPlansResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	run, err := s.planRun(ctx, req.GetRunId(), "ListPlans")
	if err != nil {
		return nil, err
	}
	plans, err := s.plans.ListPlans(ctx, run)
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "list plans of run %s: %v", run.ID, err)
	}
	return &pb.ListPlansResponse{Plans: plans}, nil
}

// ViewPlan reads one plan.
func (s *Service) ViewPlan(ctx context.Context, req *pb.ViewPlanRequest) (*pb.ViewPlanResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	run, err := s.planRun(ctx, req.GetRunId(), "ViewPlan")
	if err != nil {
		return nil, err
	}
	plan, err := s.plans.ViewPlan(ctx, run, req.GetNumber())
	switch {
	case errors.Is(err, ErrPlanNotFound):
		return nil, status.Errorf(codes.NotFound, "run %s has no plan %d: list them with ListPlans", run.ID, req.GetNumber())
	case err != nil:
		return nil, status.Errorf(codes.Unavailable, "view plan %d of run %s: %v", req.GetNumber(), run.ID, err)
	}
	return &pb.ViewPlanResponse{Plan: plan}, nil
}
