package daemon

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"time"
	"unicode/utf8"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
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
	// Directories answers ListDirectory. Nil is a host that cannot list folders.
	Directories Directories
	// Catalogue answers ListCatalogue. Nil is a host with no catalogue.
	Catalogue CatalogueSource
	// Places answers ListEvalPlaces and gives ListCatalogue the places it works the fit out from. Nil is
	// a host that cannot report places.
	Places PlaceSource
	// Provisioner answers ProvisionRepository. Nil is a host that cannot make repositories.
	Provisioner Provisioner
	// JobTool is what the agents of a run that starts now use, as the tool itself answers. It may start
	// or replace the tool's daemon, so it is asked before the command reaches the engine, whose lock it
	// must not hold, and its failure refuses the start. It is bounded by the caller's context. Nil means
	// a host with no job tool, and its runs record none.
	JobTool func(context.Context) (core.JobToolEndpoint, error)
	// Tasks reads the card of a task at start. Nil is a host with no reader, whose runs record no card.
	Tasks TaskReader
	// SetupReads reads the facts of a run's setup, afresh, when the operator completes it. Nil is a host
	// that cannot: it completes no setup.
	SetupReads SetupReads
	// HistoryPageBytes is the byte budget of one page of GetRunHistory. Zero is MaxHistoryPageBytes;
	// a smaller one lets a test fill a page with a small fleet.
	HistoryPageBytes int
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
	if deps.HistoryPageBytes <= 0 {
		deps.HistoryPageBytes = MaxHistoryPageBytes
	}
	return &Service{e: e, ServiceDeps: deps}, nil
}

// Register adds the service to a gRPC server.
func (s *Service) Register(g grpc.ServiceRegistrar) { pb.RegisterOrchestratorServiceServer(g, s) }

func (s *Service) versions() *pb.BuildVersions {
	return protocol.Build(s.Binary)
}

// Hello answers a handshake with the daemon's versions and whether the two can talk.
func (s *Service) Hello(_ context.Context, req *pb.HelloRequest) (*pb.HelloResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	return &pb.HelloResponse{Daemon: s.versions(), Compatibility: protocol.Judge(s.versions(), req.GetClient())}, nil
}

// caller says who is calling, from the call's metadata.
func (s *Service) caller(ctx context.Context) (core.Caller, error) {
	md, _ := metadata.FromIncomingContext(ctx)
	agent := md.Get(MetaAgent)
	if s.Audience == AudienceOperator {
		if len(agent)+len(md.Get(MetaSession))+len(md.Get(MetaToken)) > 0 {
			return nil, status.Error(codes.PermissionDenied, "this is the operator's socket and it takes no agent credentials: an agent's tools use the endpoint named by "+envcodec.NameOf(&pb.ToolServerCredentials{}, "endpoint"))
		}
		viewer, err := viewerOf(md)
		if err != nil {
			return nil, err
		}
		return core.Operator{Viewer: viewer}, nil
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

// maxViewerBytes bounds the id and the label of a viewer, as the contract does.
const maxViewerBytes = 128

// viewerOf reads the viewer a call's metadata names. A call that names none has none: only a command
// that needs one is refused for it (see Dispatch). A call that names half of one, or one the contract
// would refuse, is an error that names the field, because it can only be a client that is wrong.
func viewerOf(md metadata.MD) (core.Viewer, error) {
	id, label := first(md.Get(protocol.MetaViewerID)), first(md.Get(protocol.MetaViewerLabel))
	for _, f := range []struct{ field, value, other string }{
		{protocol.MetaViewerID, id, label}, {protocol.MetaViewerLabel, label, id},
	} {
		switch {
		case f.value == "" && f.other != "":
			return core.Viewer{}, fieldError(codes.InvalidArgument, f.field, "is missing: a viewer is an id and a label, and this call has only the other")
		case len(f.value) > maxViewerBytes:
			return core.Viewer{}, fieldError(codes.InvalidArgument, f.field, fmt.Sprintf("is longer than %d bytes", maxViewerBytes))
		case !utf8.ValidString(f.value):
			return core.Viewer{}, fieldError(codes.InvalidArgument, f.field, "is not valid UTF-8")
		}
	}
	return core.Viewer{ID: core.ViewerID(id), Label: label}, nil
}

// fieldError is an error that names the metadata field at fault, in the status's details as well as in
// its words, so a client can tell which field without reading the message.
func fieldError(code codes.Code, field, why string) error {
	st := status.New(code, fmt.Sprintf("the call's %s metadata %s", field, why))
	detailed, err := st.WithDetails(&errdetails.BadRequest{FieldViolations: []*errdetails.BadRequest_FieldViolation{{Field: field, Description: why}}})
	if err != nil {
		return st.Err()
	}
	return detailed.Err()
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
	if op, ok := cmd.Caller.(core.Operator); ok {
		if _, catchUp := cmd.Body.(core.RequestCatchUp); catchUp && op.Viewer == (core.Viewer{}) {
			return nil, fieldError(codes.InvalidArgument, protocol.MetaViewerID, "is missing: a catch-up is recorded for a viewer, so the call names one with this field and "+protocol.MetaViewerLabel)
		}
	}
	cmd, err = s.checkStart(ctx, cmd)
	if err != nil {
		return nil, err
	}
	cmd, err = s.checkSetup(ctx, cmd)
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

// CheckRepository answers whether a start in the repository passes the first part of the check a
// StartRun makes, without making a run, an event or a receipt. A refusal is an answer, not a failure
// of the call: the operator typed a path and is told what is wrong with it.
func (s *Service) CheckRepository(ctx context.Context, req *pb.CheckRepositoryRequest) (*pb.CheckRepositoryResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "CheckRepository"); err != nil {
		return nil, err
	}
	canonical, err := s.Starts.CheckRepository(req.GetRepository())
	var refusal *StartRefusal
	switch {
	case err == nil:
		tasks, err := s.Starts.Tasks(canonical)
		if err != nil {
			return nil, status.Error(codes.Unavailable, err.Error())
		}
		return &pb.CheckRepositoryResponse{Tasks: tasks}, nil
	case errors.As(err, &refusal):
		return &pb.CheckRepositoryResponse{Refusal: &pb.StartRefusal{Reason: refusal.Reason, NotReady: refusal.NotReady}}, nil
	}
	return nil, status.Error(codes.Unavailable, err.Error())
}

// ListDirectory lists the children of one folder of the host, for a path being typed. A path that
// cannot be listed is the operator's to fix, so its refusal names the path and the field.
func (s *Service) ListDirectory(ctx context.Context, req *pb.ListDirectoryRequest) (*pb.ListDirectoryResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "ListDirectory"); err != nil {
		return nil, err
	}
	if s.Directories == nil {
		return nil, status.Error(codes.Unimplemented, "this host cannot list folders")
	}
	listing, err := bound(s.Bounds, ctx, func(c context.Context) (Listing, error) {
		return s.Directories.List(c, req.GetPath(), req.GetIncludeHidden())
	})
	var fault *DirectoryError
	switch {
	case errors.As(err, &fault):
		return nil, status.Error(directoryCode(fault.Fault), fault.Reason)
	case err != nil:
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	entries := make([]*pb.DirectoryEntry, len(listing.Entries))
	for i, e := range listing.Entries {
		entries[i] = &pb.DirectoryEntry{Name: e.Name, IsDirectory: e.IsDirectory, IsGitRepository: e.IsGitRepository, HasTasks: e.HasTasks}
	}
	return &pb.ListDirectoryResponse{Entries: entries, Truncated: listing.Truncated}, nil
}

// directoryCode is the status code of a folder that cannot be listed.
func directoryCode(f DirectoryFault) codes.Code {
	switch f {
	case DirectoryNotFound:
		return codes.NotFound
	case DirectoryDenied:
		return codes.PermissionDenied
	default:
		return codes.InvalidArgument
	}
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
	if err == nil {
		start.Repository = canonical
		if start.Facts, err = s.startFacts(ctx, start); err != nil {
			return cmd, err
		}
		if start.Facts.Refused == nil && s.JobTool != nil {
			start.JobTool, err = s.JobTool(ctx)
			if err != nil {
				return cmd, status.Error(codes.FailedPrecondition, err.Error())
			}
		}
		cmd.Body = start
		return cmd, nil
	}
	return cmd, startError(err)
}

// startError is the status a refused or failed start check answers with.
func startError(err error) error {
	var refusal *StartRefusal
	switch {
	case errors.As(err, &refusal) && refusal.NotReady:
		return status.Error(codes.FailedPrecondition, refusal.Reason)
	case errors.As(err, &refusal):
		return status.Error(codes.InvalidArgument, refusal.Reason)
	}
	return status.Error(codes.Unavailable, err.Error())
}

// runOf is the run an agent caller belongs to.
func (s *Service) runOf(c core.Caller) core.RunID {
	if a, ok := c.(core.AgentCaller); ok {
		agent, _ := s.e.State().Agent(a.Agent)
		return agent.Run
	}
	return ""
}

// WatchRunsFrom opens the stream behind WatchRuns with whole histories in its snapshot, for callers that
// poll it.
func (s *Service) WatchRunsFrom(caller core.Caller, after uint64) (*RunsWatch, []*pb.WatchRunsResponse, error) {
	return s.WatchRunsDetailFrom(caller, after, pb.SnapshotDetail_SNAPSHOT_DETAIL_UNSPECIFIED)
}

// WatchRunsDetailFrom opens the stream behind WatchRuns. With detail SUMMARY the stream is also the one
// that sends RunSummary frames: a client that asked for the summaries is one that reads them, and a
// reader that did not ask would not know the frame.
func (s *Service) WatchRunsDetailFrom(caller core.Caller, after uint64, detail pb.SnapshotDetail) (*RunsWatch, []*pb.WatchRunsResponse, error) {
	st := newRunsStream(s, caller, detail)
	w := st.watch()
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
	records, err := s.e.readMessages(run)
	if err != nil {
		return err
	}
	for _, rec := range records {
		snap.Messages = append(snap.Messages, wire.MailboxMessage(rec.Message))
	}
	return nil
}

// WatchRuns streams a snapshot of runs and agents, then the public part of every event after it.
func (s *Service) WatchRuns(req *pb.WatchRunsRequest, stream grpc.ServerStreamingServer[pb.WatchRunsResponse]) error {
	caller, err := s.caller(stream.Context())
	if err != nil {
		return err
	}
	if err := wire.Validate(req); err != nil {
		return status.Error(codes.InvalidArgument, err.Error())
	}
	w, first, err := s.WatchRunsDetailFrom(caller, req.GetAfterSequence(), req.GetDetail())
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
		change: func(c *pb.RunChange) (*pb.WatchAgentResponse, bool) {
			switch b := c.GetBody().(type) {
			case *pb.RunChange_Item:
				if b.Item.GetAgentId() == string(agent) {
					return &pb.WatchAgentResponse{Body: &pb.WatchAgentResponse_Item{Item: b.Item}}, true
				}
			case *pb.RunChange_Turn:
				if b.Turn.GetAgentId() == string(agent) {
					return &pb.WatchAgentResponse{Body: &pb.WatchAgentResponse_Turn{Turn: b.Turn}}, true
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

// ElideOver is the size in bytes above which a page of history leaves out a reasoning text or a
// tool call's output (ItemsPage.elisions), so that a page of an agent that thought and ran a lot
// stays small. The one place the limit is written; GetItem reads what a page left out.
const ElideOver = 4096

// elide empties a reasoning text or a tool call's output larger than ElideOver, and returns what
// it says of it: the item's ordinal and how large the text was. Assistant text, prompts and what a
// tool was asked are never left out. It is for the items of a page, which are the caller's own
// copies: an item that arrives in a stream is never passed to it.
func elide(item *pb.Item) *pb.Elision {
	var text *string
	switch body := item.GetBody().(type) {
	case *pb.Item_Reasoning:
		text = &body.Reasoning.Text
	case *pb.Item_Tool:
		text = &body.Tool.Output
	default:
		return nil
	}
	size := len(*text)
	if size <= ElideOver {
		return nil
	}
	*text = ""
	return &pb.Elision{Ordinal: item.GetOrdinal(), BodyBytes: uint64(size)}
}

// itemsPage reads a page of an agent's items. A zero before reads the newest. Large text is left out.
func (s *Service) itemsPage(agent core.AgentID, before uint32, size int) (*pb.ItemsPage, error) {
	page, err := s.e.Store.Items(agent, before, min(size, store.MaxPageItems))
	if err != nil {
		return nil, fmt.Errorf("read items of %s: %w", agent, err)
	}
	out := &pb.ItemsPage{}
	for _, it := range page.Items {
		item := wire.ItemMessage(it)
		if e := elide(item); e != nil {
			out.Elisions = append(out.Elisions, e)
		}
		out.Items = append(out.Items, item)
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

// GetItem reads one item whole, as it stands: what a page of history elided.
func (s *Service) GetItem(ctx context.Context, req *pb.GetItemRequest) (*pb.GetItemResponse, error) {
	if err := wire.Validate(req); err != nil {
		return nil, status.Error(codes.InvalidArgument, err.Error())
	}
	caller, err := s.caller(ctx)
	if err != nil {
		return nil, err
	}
	if err := operatorOnly(caller, "GetItem"); err != nil {
		return nil, err
	}
	agent, ordinal := core.AgentID(req.GetAgentId()), req.GetOrdinal()
	if _, ok := s.e.State().Agent(agent); !ok {
		return nil, status.Errorf(codes.NotFound, "no agent %s: list the agents of a run with WatchRuns", agent)
	}
	var resp *pb.GetItemResponse
	if err := s.e.Cut(func(_ core.State, last uint64) error {
		// The newest item below the next ordinal is the item itself when it exists. The largest
		// ordinal has no next one (a bound of zero reads the newest), and no agent has that many items.
		if ordinal == ^uint32(0) {
			return nil
		}
		page, err := s.e.Store.Items(agent, ordinal+1, 1)
		if err != nil {
			return fmt.Errorf("read item %d of %s: %w", ordinal, agent, err)
		}
		if len(page.Items) == 1 && page.Items[0].Ordinal == ordinal {
			resp = &pb.GetItemResponse{Item: wire.ItemMessage(page.Items[0]), Sequence: last}
		}
		return nil
	}); err != nil {
		return nil, status.Error(codes.Unavailable, err.Error())
	}
	if resp == nil {
		return nil, status.Errorf(codes.NotFound, "agent %s has no item %d: its items start at 1", agent, ordinal)
	}
	return resp, nil
}
