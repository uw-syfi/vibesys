// Package client is the way to the daemon. It speaks only OrchestratorService, so the desktop app
// and vibesys-client (orchestrator/clientcli) use exactly the same calls, and an agent's tools use
// them too, with the session metadata that says which agent is calling.
//
// There is one transport: a net.Conn from a Dialer. On the daemon's host the Dialer opens the
// gateway's Unix socket. The package starts no process and opens no socket itself.
package client

import (
	"context"
	"errors"
	"fmt"
	"net"
	"time"

	"google.golang.org/genproto/googleapis/rpc/errdetails"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/types/known/timestamppb"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/protocol"
)

// Dialer opens one connection to the daemon. It is called again after a connection breaks.
type Dialer func(ctx context.Context) (net.Conn, error)

// IDs mints the id of each command. An id is the key of a command's receipt, so a retry reuses the
// id it was given, and a new command gets a new one.
type IDs interface{ NewCommandID() string }

// Session says which agent session a client speaks for. The daemon checks the token.
type Session struct {
	Agent   string
	Session uint32
	Token   string
}

// Viewer says which install of an operator's client is calling: an id that stays the same for the
// install, and a name to show. The daemon takes both as a label, to say whose a check-in is, and
// authenticates neither.
type Viewer struct {
	ID    string
	Label string
}

// Options are what Dial needs besides a Dialer.
type Options struct {
	// BinaryVersion is the release version, for display.
	BinaryVersion string
	// Session is set by an agent's tools and left empty by an operator.
	Session *Session
	// Viewer is set by an operator's client, and is sent on every call. A call to catch up needs it, and
	// the daemon refuses one without it, naming the field (see BadField).
	Viewer *Viewer
}

// Client is a connection to the daemon.
type Client struct {
	conn *grpc.ClientConn
	svc  pb.OrchestratorServiceClient
	// Daemon is what the daemon said about itself in the handshake.
	Daemon *pb.BuildVersions
}

// IncompatibleError says the handshake found the two builds unable to talk.
type IncompatibleError struct {
	Compatibility pb.Compatibility
	Daemon        *pb.BuildVersions
}

func (e *IncompatibleError) Error() string {
	switch e.Compatibility {
	case pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD:
		return fmt.Sprintf("this client (protocol older than %d) is too old for the host (%s): update this client to the host's version, or replace the host with this client's with `vibesys-client host restart`",
			e.Daemon.GetMinProtocolVersion(), e.Daemon.GetBinaryVersion())
	default:
		return fmt.Sprintf("the host (%s, protocol %d) is older than this client needs: replace it with `vibesys-client host restart`",
			e.Daemon.GetBinaryVersion(), e.Daemon.GetProtocolVersion())
	}
}

// Dial connects and performs the version handshake. A daemon this client cannot talk to is an
// IncompatibleError that says which side to update.
func Dial(ctx context.Context, dial Dialer, opts Options) (*Client, error) {
	dialOpts := []grpc.DialOption{
		grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(MaxReceiveMessageBytes)),
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return dial(ctx) }),
	}
	var pairs []string
	if s := opts.Session; s != nil {
		pairs = append(pairs, "vibesys-agent", s.Agent, "vibesys-session", fmt.Sprint(s.Session), "vibesys-token", s.Token)
	}
	if v := opts.Viewer; v != nil {
		pairs = append(pairs, protocol.MetaViewerID, v.ID, protocol.MetaViewerLabel, v.Label)
	}
	if len(pairs) > 0 {
		md := metadata.Pairs(pairs...)
		dialOpts = append(dialOpts,
			grpc.WithChainUnaryInterceptor(func(ctx context.Context, method string, req, reply any, cc *grpc.ClientConn, inv grpc.UnaryInvoker, o ...grpc.CallOption) error {
				return inv(metadata.NewOutgoingContext(ctx, md), method, req, reply, cc, o...)
			}),
			grpc.WithChainStreamInterceptor(func(ctx context.Context, d *grpc.StreamDesc, cc *grpc.ClientConn, method string, st grpc.Streamer, o ...grpc.CallOption) (grpc.ClientStream, error) {
				return st(metadata.NewOutgoingContext(ctx, md), d, cc, method, o...)
			}))
	}
	conn, err := grpc.NewClient("passthrough:///orchestrator", dialOpts...)
	if err != nil {
		return nil, fmt.Errorf("connect to the orchestrator: %w", err)
	}
	c := &Client{conn: conn, svc: pb.NewOrchestratorServiceClient(conn)}
	resp, err := c.svc.Hello(ctx, &pb.HelloRequest{Client: protocol.Build(opts.BinaryVersion)})
	if err != nil {
		_ = conn.Close()
		return nil, fmt.Errorf("handshake with the orchestrator: %w", err)
	}
	c.Daemon = resp.GetDaemon()
	if resp.GetCompatibility() != pb.Compatibility_COMPATIBILITY_COMPATIBLE {
		_ = conn.Close()
		return nil, &IncompatibleError{Compatibility: resp.GetCompatibility(), Daemon: resp.GetDaemon()}
	}
	return c, nil
}

// Close ends the connection.
func (c *Client) Close() error { return c.conn.Close() }

// BadField names the field of the call that an error says is wrong or missing, such as the viewer
// metadata a catch-up needs, and reports false for an error that names none.
func BadField(err error) (string, bool) {
	st, ok := status.FromError(err)
	if !ok {
		return "", false
	}
	for _, d := range st.Details() {
		if bad, ok := d.(*errdetails.BadRequest); ok && len(bad.GetFieldViolations()) > 0 {
			return bad.GetFieldViolations()[0].GetField(), true
		}
	}
	return "", false
}

// Dispatch carries out a command. A retry with the same command gets the same receipt.
func (c *Client) Dispatch(ctx context.Context, cmd *pb.Command) (*pb.Receipt, error) {
	resp, err := c.svc.Dispatch(ctx, &pb.DispatchRequest{Command: cmd})
	if err != nil {
		return nil, err
	}
	return resp.GetReceipt(), nil
}

// WatchRuns opens the stream of runs: a snapshot, then changes after it. After zero starts with a
// snapshot; otherwise it continues after that sequence.
func (c *Client) WatchRuns(ctx context.Context, after uint64) (grpc.ServerStreamingClient[pb.WatchRunsResponse], error) {
	return c.svc.WatchRuns(ctx, &pb.WatchRunsRequest{AfterSequence: after})
}

// WatchRunsSummary opens the stream of runs with a snapshot of detail SUMMARY: the runs and their
// summaries, and a summary again each time one changes. A run's history is read with GetRunHistory.
func (c *Client) WatchRunsSummary(ctx context.Context, after uint64) (grpc.ServerStreamingClient[pb.WatchRunsResponse], error) {
	return c.svc.WatchRuns(ctx, &pb.WatchRunsRequest{AfterSequence: after, Detail: pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY})
}

// GetRunHistory reads one page of a run's history: the agents, turns and check-ins on the first page
// (cursor zero) and the messages on every page, until a page's next cursor is zero.
func (c *Client) GetRunHistory(ctx context.Context, run string, cursor uint64) (*pb.GetRunHistoryResponse, error) {
	return c.svc.GetRunHistory(ctx, &pb.GetRunHistoryRequest{RunId: run, Cursor: cursor})
}

// WatchAgent opens the stream of one agent: a page of its newest items, then items, turns and
// deltas.
func (c *Client) WatchAgent(ctx context.Context, agent string, pageSize uint32, after uint64) (grpc.ServerStreamingClient[pb.WatchAgentResponse], error) {
	return c.svc.WatchAgent(ctx, &pb.WatchAgentRequest{AgentId: agent, PageSize: pageSize, AfterSequence: after})
}

// WatchRunCost opens the stream of what one run has cost: its whole cost view, then the whole view
// again each time it changes. A client keeps the last view and combines none.
func (c *Client) WatchRunCost(ctx context.Context, run string) (grpc.ServerStreamingClient[pb.WatchRunCostResponse], error) {
	return c.svc.WatchRunCost(ctx, &pb.WatchRunCostRequest{RunId: run})
}

// WatchHostCost opens the stream of what every run the host holds has cost: its whole cost view, then
// the whole view again each time any run's cost changes. A client keeps the last view and combines
// none. The zero since counts everything; otherwise only the turns that finished at or after it.
func (c *Client) WatchHostCost(ctx context.Context, since time.Time) (grpc.ServerStreamingClient[pb.WatchHostCostResponse], error) {
	req := &pb.WatchHostCostRequest{}
	if !since.IsZero() {
		req.Since = timestamppb.New(since)
	}
	return c.svc.WatchHostCost(ctx, req)
}

// ListItems reads older items of an agent.
func (c *Client) ListItems(ctx context.Context, agent string, before, pageSize uint32) (*pb.ItemsPage, error) {
	resp, err := c.svc.ListItems(ctx, &pb.ListItemsRequest{AgentId: agent, BeforeOrdinal: before, PageSize: pageSize})
	if err != nil {
		return nil, err
	}
	return resp.GetPage(), nil
}

// Snapshot reads the runs and agents as they stand: the first message of WatchRuns.
func (c *Client) Snapshot(ctx context.Context) (*pb.RunsSnapshot, error) {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	stream, err := c.WatchRuns(ctx, 0)
	if err != nil {
		return nil, err
	}
	msg, err := stream.Recv()
	if err != nil {
		return nil, err
	}
	snapshot, _, _, classifyErr := classifyWatchRuns(msg)
	if classifyErr != nil || snapshot == nil {
		return nil, errors.New("the orchestrator began a stream of runs with something other than a snapshot")
	}
	return snapshot, nil
}

// Agents lists the agents the daemon shows this caller: all of them for an operator, those of its
// own run for an agent.
func (c *Client) Agents(ctx context.Context) ([]*pb.Agent, error) {
	snap, err := c.Snapshot(ctx)
	if err != nil {
		return nil, err
	}
	return snap.GetAgents(), nil
}
