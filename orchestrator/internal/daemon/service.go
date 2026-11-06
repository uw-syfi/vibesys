package daemon

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	"github.com/uw-syfi/vibesys/cli/envcodec"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/hub"
	"github.com/uw-syfi/vibesys/orchestrator/internal/protocol"
	"github.com/uw-syfi/vibesys/orchestrator/internal/store"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// endOfTime is after every turn, for a read of all of a run's turns.
var endOfTime = time.Date(9999, time.December, 31, 0, 0, 0, 0, time.UTC)

// The metadata an agent's tools put on every call. Together they say which session is calling; the
// daemon checks the token and never reads an agent id from a request body.
const (
	MetaAgent   = "vibesys-agent"
	MetaSession = "vibesys-session"
	MetaToken   = "vibesys-token"
)

// Audience says who a Service is for. The daemon serves two sockets with one Service each, so that
// who may call is settled by which socket was reached and not by what the caller claims.
type Audience int

const (
	// AudienceOperator is the operator's socket. A call that carries an agent's credentials is
	// refused there, so a tool that was launched with the wrong socket fails loudly.
	AudienceOperator Audience = iota + 1
	// AudienceAgent is the agent-only socket, the only one an agent's environment names. Every call
	// needs the credentials of the agent's current session, and the calls open to an agent are its
	// commands and the snapshot of its run: nothing else.
	AudienceAgent
)

// ServiceDeps is what a Service is made of.
type ServiceDeps struct {
	Authority Authority
	Audience  Audience
	// Bounds limits the calls the service makes to the plan reader.
	Bounds Bounds
	// Binary is the release version, for display.
	Binary string
	// Starts refuses a StartRun that cannot work before it is decided.
	Starts StartChecks
	// JobTool is what the agents of a run that starts now use, as the tool itself answers. It may start
	// or replace the tool's daemon, so it is asked before the command reaches the engine, whose lock it
	// must not hold, and its failure refuses the start. It is bounded by the caller's context. Nil means
	// a host with no job tool, and its runs record none.
	JobTool func(context.Context) (core.JobToolEndpoint, error)
}

// Service is OrchestratorService over an Engine and the hub, for one Audience.
type Service struct {
	pb.UnimplementedOrchestratorServiceServer
	e *Engine
	ServiceDeps
}

// NewService returns the service.
func NewService(e *Engine, deps ServiceDeps) (*Service, error) {
	if deps.Audience != AudienceOperator && deps.Audience != AudienceAgent {
		return nil, errors.New("daemon: a Service needs an Audience")
	}
	if deps.Bounds == nil {
		return nil, errors.New("daemon: ServiceDeps.Bounds is required")
	}
	if deps.Starts == nil {
		return nil, errors.New("daemon: ServiceDeps.Starts is required")
	}
	return &Service{e: e, ServiceDeps: deps}, nil
}

// Register adds the service to a gRPC server.
func (s *Service) Register(g grpc.ServiceRegistrar) { pb.RegisterOrchestratorServiceServer(g, s) }

func (s *Service) versions() *pb.BuildVersions {
	return &pb.BuildVersions{BinaryVersion: s.Binary, ProtocolVersion: protocol.Version, MinProtocolVersion: protocol.MinVersion}
}

// Hello answers a handshake with the daemon's versions and whether the two can talk.
func (s *Service) Hello(_ context.Context, req *pb.HelloRequest) (*pb.HelloResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	c := req.GetClient()
	compat := pb.Compatibility_COMPATIBILITY_COMPATIBLE
	switch {
	case c.GetProtocolVersion() < protocol.MinVersion:
		compat = pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD
	case c.GetMinProtocolVersion() > protocol.Version:
		compat = pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD
	}
	return &pb.HelloResponse{Daemon: s.versions(), Compatibility: compat}, nil
}

// caller says who is calling, from the call's metadata.
func (s *Service) caller(ctx context.Context) (core.Caller, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	agent := md.Get(MetaAgent)
	if s.Audience == AudienceOperator {
		if len(agent)+len(md.Get(MetaSession))+len(md.Get(MetaToken)) > 0 {
			return nil, status.Error(codes.PermissionDenied, "this is the operator's socket and it takes no agent credentials: an agent's tools use the endpoint named by "+envcodec.NameOf(&pb.ToolServerCredentials{}, "endpoint"))
		}
		return core.Operator{}, nil
	}
	if len(agent) == 0 {
		return nil, status.Error(codes.Unauthenticated, "this is the agent socket and every call needs the session credentials: start the tool from the environment the orchestrator launched the agent with")
	}
	n, err := strconv.ParseUint(first(md.Get(MetaSession)), 10, 32)
	session := core.Session{Agent: core.AgentID(agent[0]), N: uint32(n)}
	if err != nil || !s.Authority.Valid(session, first(md.Get(MetaToken))) {
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
	cmd, err = s.checkStart(ctx, cmd)
	if err != nil {
		return nil, err
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

// checkStart refuses a new StartRun whose repository, task or launcher cannot work, before the
// command reaches Decide, and names its repository in canonical form, so that the one-run rule of
// Decide holds however the repository was spelled. The command's digest stays that of what the
// client sent, so a retry of it is the same command. A retry of a command that already has a
// receipt skips the check, so it still gets that receipt.
func (s *Service) checkStart(ctx context.Context, cmd core.Command) (core.Command, error) {
	start, ok := cmd.Body.(core.StartRun)
	if _, operator := cmd.Caller.(core.Operator); !ok || !operator {
		// Only the operator may start a run; Decide refuses an agent that tries, with a receipt.
		return cmd, nil
	}
	if _, recall := s.e.State().Recall(cmd.ID, cmd.Digest); recall != core.RecallNew {
		return cmd, nil
	}
	canonical, err := s.Starts.CheckStart(start.Repository, start.Task)
	var refusal *StartRefusal
	switch {
	case err == nil:
		start.Repository = canonical
		if s.JobTool != nil {
			start.JobTool, err = s.JobTool(ctx)
			if err != nil {
				return cmd, status.Error(codes.FailedPrecondition, err.Error())
			}
		}
		cmd.Body = start
		return cmd, nil
	case errors.As(err, &refusal) && refusal.NotReady:
		return cmd, status.Error(codes.FailedPrecondition, refusal.Reason)
	case errors.As(err, &refusal):
		return cmd, status.Error(codes.InvalidArgument, refusal.Reason)
	}
	return cmd, status.Error(codes.Unavailable, err.Error())
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
	// One goroutine owns open, Poll and resync for a watch, including this cursor.
	seen := map[string]uint64{}
	latest := func(search *pb.SearchState) (*pb.WatchRunsResponse, bool) {
		if scoped && core.RunID(search.RunId) != run || search.Revision <= seen[search.RunId] {
			return nil, false
		}
		seen[search.RunId] = search.Revision
		return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Search{Search: search}}, true
	}
	w := &RunsWatch{
		e: s.e, run: run, maxBytes: MaxWatchFrameBytes,
		snapshot: func(state core.State, last uint64) (*pb.WatchRunsResponse, error) {
			return s.runsSnapshot(state, last, run, scoped, seen)
		},
		event: func(ev *pb.Event) (*pb.WatchRunsResponse, bool) {
			if scoped && core.RunID(ev.GetRunId()) != run {
				return nil, false
			}
			return &pb.WatchRunsResponse{Body: &pb.WatchRunsResponse_Event{Event: ev}}, true
		},
		delta:  func(*pb.Delta) (*pb.WatchRunsResponse, bool) { return nil, false },
		latest: latest,
		current: func(state core.State) []*pb.WatchRunsResponse {
			var out []*pb.WatchRunsResponse
			for _, r := range state.Runs() {
				if msg, ok := latest(s.e.searchState(r.ID)); ok {
					out = append(out, msg)
				}
			}
			return out
		},
	}
	first, err := w.open(hub.Interest{Everything: true}, after)
	return w, first, err
}

// addHistory adds a run's turns and messages to a snapshot, as the contract promises, so that a
// client that connects after a report was sent still shows it. It reads inside the snapshot's Cut,
// where nothing commits, so the rows it reads are those of the snapshot's sequence.
func (s *Service) addHistory(snap *pb.RunsSnapshot, run core.RunID) error {
	window, err := s.e.Store.Turns(run, time.Time{}, endOfTime, store.MaxWindowTurns)
	if err != nil {
		return fmt.Errorf("read the turns of %s: %w", run, err)
	}
	// A run with more turns than the window holds shows its earliest; the timeline pages the rest.
	for _, t := range window.Turns {
		snap.Turns = append(snap.Turns, wire.TurnMessage(t))
	}
	for after := uint64(0); ; {
		page, err := s.e.Store.Messages(run, after, store.MaxPageMessages)
		if err != nil {
			return fmt.Errorf("read the messages of %s: %w", run, err)
		}
		for _, rec := range page {
			snap.Messages = append(snap.Messages, wire.MailboxMessage(rec.Message))
			after = rec.Seq
		}
		if len(page) < store.MaxPageMessages {
			return nil
		}
	}
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
