package client

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sync"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Control is a client of a host's HostControlService. It makes no handshake: the service is the one
// any release answers, so it is how a client finds out what it has reached.
type Control struct {
	conn *grpc.ClientConn
	svc  pb.HostControlServiceClient
}

// DialControl speaks to a host over a connection already made, which is how the caller knows a host
// is there before it asks anything. The connection serves one use: it is not made again if it breaks.
func DialControl(connection net.Conn) (*Control, error) {
	var once sync.Once
	conn, err := grpc.NewClient("passthrough:///host", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
			var first net.Conn
			once.Do(func() { first = connection })
			if first == nil {
				return nil, errors.New("the connection to the host was used up")
			}
			return first, nil
		}))
	if err != nil {
		return nil, fmt.Errorf("connect to the host: %w", err)
	}
	return &Control{conn: conn, svc: pb.NewHostControlServiceClient(conn)}, nil
}

// Close ends the connection.
func (c *Control) Close() error { return c.conn.Close() }

// Identify asks the host what it is. clientRelease is this client's release, which the host
// compares with its own.
func (c *Control) Identify(ctx context.Context, clientRelease string) (*pb.IdentifyResponse, error) {
	return c.svc.Identify(ctx, &pb.IdentifyRequest{ClientRelease: clientRelease})
}

// Stop asks the host to end.
func (c *Control) Stop(ctx context.Context, onlyIfIdle bool) (*pb.StopResponse, error) {
	return c.svc.Stop(ctx, &pb.StopRequest{OnlyIfIdle: onlyIfIdle})
}
