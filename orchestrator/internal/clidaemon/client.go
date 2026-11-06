package clidaemon

import (
	"context"
	"errors"
	"fmt"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"net"
	"time"
)

// RequiredDaemonProtocol is pinned against the callee's protocol.json.
const RequiredDaemonProtocol uint32 = 3

// maxReceiveBytes accommodates 500 maximum-size 256 KiB plans plus encoding overhead.
const maxReceiveBytes = 256 << 20

var _ Connection = (*Client)(nil)

// Stream receives semantic task facts; cancellation belongs to its opener.
type Stream interface {
	Recv() (*pb.WatchTaskResponse, error)
}

// Service is the narrow, read-only contract used by a follower. No store reads
// or CLI text are exposed. The CLI contract currently has no command IDs, so
// this link deliberately exposes no mutation RPCs.
type Service interface {
	Hello(context.Context, *pb.HelloRequest) (*pb.HelloResponse, error)
	WatchTask(context.Context, *pb.WatchTaskRequest) (Stream, error)
}

// Client adapts generated stubs, with transport ownership at this boundary.
type Client struct {
	conn *grpc.ClientConn
	svc  pb.DaemonServiceClient
}

// Dialer opens a checked socket. The production implementation owns real I/O.
type Dialer interface {
	Dial(context.Context, Socket) (net.Conn, error)
}

func NewClient(socket Socket, dial Dialer) (*Client, error) {
	if socket.path == "" {
		return nil, &Unreadable{core.NoDaemon, "socket was not located"}
	}
	conn, err := grpc.NewClient("passthrough:///cli", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(maxReceiveBytes)), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return dial.Dial(ctx, socket) }))
	if err != nil {
		return nil, err
	}
	return &Client{conn, pb.NewDaemonServiceClient(conn)}, nil
}
func (c *Client) Close() error { return c.conn.Close() }
func (c *Client) Hello(ctx context.Context, r *pb.HelloRequest) (*pb.HelloResponse, error) {
	if _, ok := ctx.Deadline(); !ok {
		return nil, fmt.Errorf("Hello requires a caller deadline")
	}
	return c.svc.Hello(ctx, r)
}
func (c *Client) WatchTask(ctx context.Context, r *pb.WatchTaskRequest) (Stream, error) {
	return c.svc.WatchTask(ctx, r)
}

// Hello is a bounded protocol-3 handshake. The bound must come from the caller,
// including when Service is a Fake; an unbounded context is rejected.
func Hello(ctx context.Context, svc Service) error {
	if _, ok := ctx.Deadline(); !ok {
		return fmt.Errorf("Hello requires a caller deadline")
	}
	r, err := svc.Hello(ctx, &pb.HelloRequest{Component: pb.Component_COMPONENT_ORCHESTRATOR, Versions: &pb.BuildVersions{ProtocolVersion: RequiredDaemonProtocol, MinProtocolVersion: RequiredDaemonProtocol}})
	if err != nil {
		reason := core.NoDaemon
		// Protocol 2 rejects COMPONENT_ORCHESTRATOR before negotiating versions.
		if status.Code(err) == codes.InvalidArgument {
			reason = core.DaemonTooOld
		}
		return &Unreadable{reason, err.Error()}
	}
	switch r.GetCompatibility() {
	case pb.Compatibility_COMPATIBILITY_COMPATIBLE:
		if r.GetVersions().GetProtocolVersion() >= RequiredDaemonProtocol && r.GetVersions().GetMinProtocolVersion() <= RequiredDaemonProtocol {
			return nil
		}
		return &Unreadable{core.DaemonTooOld, "daemon returned inconsistent versions"}
	case pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD:
		return &Unreadable{core.ClientTooOld, "CLI daemon needs a newer client"}
	case pb.Compatibility_COMPATIBILITY_DAEMON_TOO_OLD:
		return &Unreadable{core.DaemonTooOld, "CLI daemon is too old"}
	default:
		return &Unreadable{core.NoDaemon, "CLI daemon returned no compatibility"}
	}
}

// Ensurer runs the sibling CLI's daemon ensure command, reading only its exit
// status. The production implementation is in PR 7 and never searches PATH.
type Ensurer interface {
	Ensure(context.Context, Location) error
}

// Ensure invokes the callee's start command once, only for a missing/old daemon.
// It never retries a start, or stops a shared daemon.
func Ensure(ctx context.Context, runner Ensurer, loc Location, reason core.ViewReason) error {
	if reason != core.NoDaemon && reason != core.DaemonTooOld {
		return &Unreadable{reason, "daemon cannot be repaired by ensure"}
	}
	if _, ok := ctx.Deadline(); !ok {
		return fmt.Errorf("Ensure requires a caller deadline")
	}
	if err := runner.Ensure(ctx, loc); err != nil {
		var unreadable *Unreadable
		if errors.As(err, &unreadable) && unreadable.Reason == core.CLINotInstalled {
			return unreadable
		}
		return &Unreadable{core.StartFailed, err.Error()}
	}
	return nil
}

// Sleep is injected by the shell; tests advance it without real time.
type Sleep func(context.Context, time.Duration) error

// Backoff is pure, capped exponential reconnection delay.
func Backoff(attempt uint) time.Duration {
	if attempt > 6 {
		attempt = 6
	}
	return time.Second * time.Duration(1<<attempt)
}
