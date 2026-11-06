// Package client is the way to the daemon. It speaks only OrchestratorService, so the desktop app
// and vibesys-client (orchestrator/clientcli) use exactly the same calls, and an agent's tools use
// them too, with the session metadata that says which agent is calling.
//
// There is one transport: a net.Conn from a Dialer. On the daemon's host the Dialer opens the
// gateway's Unix socket. A Dialer for another host would run `ssh host vibesys-host connect`, which
// pipes its stdio to that socket, and wrap the pipes with StdioConn; none is wired today. The
// package starts no process and opens no socket itself.
package client

import (
	"context"
	"errors"
	"fmt"
	"net"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/metadata"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// MaxReceiveMessageBytes is the explicit receive limit for the orchestrator link.
const MaxReceiveMessageBytes = 4 << 20

// The versions this build speaks, sent in the handshake. They equal the daemon package's, which the
// client does not import; a test holds the two together.
const (
	ProtocolVersion    uint32 = 1
	MinProtocolVersion uint32 = 1
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

// Options are what Dial needs besides a Dialer.
type Options struct {
	// BinaryVersion is the release version, for display.
	BinaryVersion string
	// Session is set by an agent's tools and left empty by an operator.
	Session *Session
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
		return fmt.Sprintf("this client (protocol older than %d) is too old for the daemon (%s): update this client to the daemon's version",
			e.Daemon.GetMinProtocolVersion(), e.Daemon.GetBinaryVersion())
	default:
		return fmt.Sprintf("the daemon (%s, protocol %d) is older than this client needs: stop the daemon process on that host so the next connection starts the current one",
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
	if s := opts.Session; s != nil {
		md := metadata.Pairs("vibesys-agent", s.Agent, "vibesys-session", fmt.Sprint(s.Session), "vibesys-token", s.Token)
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
	resp, err := c.svc.Hello(ctx, &pb.HelloRequest{Client: &pb.BuildVersions{
		BinaryVersion: opts.BinaryVersion, ProtocolVersion: ProtocolVersion, MinProtocolVersion: MinProtocolVersion,
	}})
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

// Dispatch carries out a command. A retry with the same command gets the same receipt.
func (c *Client) Dispatch(ctx context.Context, cmd *pb.Command) (*pb.Receipt, error) {
	resp, err := c.svc.Dispatch(ctx, &pb.DispatchRequest{Command: cmd})
	if err != nil {
		return nil, err
	}
	return resp.GetReceipt(), nil
}

// WatchRuns opens the stream of runs: a snapshot, then events after it. After zero starts with a
// snapshot; otherwise it continues after that sequence.
func (c *Client) WatchRuns(ctx context.Context, after uint64) (grpc.ServerStreamingClient[pb.WatchRunsResponse], error) {
	return c.svc.WatchRuns(ctx, &pb.WatchRunsRequest{AfterSequence: after})
}

// WatchAgent opens the stream of one agent: a page of its newest items, then items, turns and
// deltas.
func (c *Client) WatchAgent(ctx context.Context, agent string, pageSize uint32, after uint64) (grpc.ServerStreamingClient[pb.WatchAgentResponse], error) {
	return c.svc.WatchAgent(ctx, &pb.WatchAgentRequest{AgentId: agent, PageSize: pageSize, AfterSequence: after})
}

// ListItems reads older items of an agent.
func (c *Client) ListItems(ctx context.Context, agent string, before, pageSize uint32) (*pb.ItemsPage, error) {
	resp, err := c.svc.ListItems(ctx, &pb.ListItemsRequest{AgentId: agent, BeforeOrdinal: before, PageSize: pageSize})
	if err != nil {
		return nil, err
	}
	return resp.GetPage(), nil
}

// ListPlans reads a run's plans.
func (c *Client) ListPlans(ctx context.Context, run string) ([]*pb.Plan, error) {
	resp, err := c.svc.ListPlans(ctx, &pb.ListPlansRequest{RunId: run})
	if err != nil {
		return nil, err
	}
	return resp.GetPlans(), nil
}

// ViewPlan reads one plan.
func (c *Client) ViewPlan(ctx context.Context, run string, number uint32) (*pb.Plan, error) {
	resp, err := c.svc.ViewPlan(ctx, &pb.ViewPlanRequest{RunId: run, Number: number})
	if err != nil {
		return nil, err
	}
	return resp.GetPlan(), nil
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
