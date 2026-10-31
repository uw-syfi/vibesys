package server

import (
	"context"
	"runtime"
	"sync/atomic"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"google.golang.org/grpc/stats"
)

const (
	// KeepaliveTime is how long a silent connection goes before a ping. A
	// half-open connection would otherwise hold its streams, and so their
	// producers, forever.
	KeepaliveTime = 30 * time.Second
	// KeepaliveTimeout is how long a ping waits for its acknowledgement.
	KeepaliveTimeout = 10 * time.Second
)

func newTopics(h *host) *subscription.Hub {
	return subscription.New(h.config.Clock,
		machinestats.Topic(runtime.GOOS, h.config.Clock, h.config.Machine),
		runsTopic(h, newRunWatch),
	)
}

// Subscribe is the wire end of the hub. A failed frame ends the stream normally,
// because the failure is data the client's state machine classifies.
func (h *host) Subscribe(request *apiv1.SubscribeRequest, stream apiv1.HostService_SubscribeServer) error {
	ctx := stream.Context()
	connection, _ := ctx.Value(connectionKey{}).(uint64)
	err := h.topics.Serve(ctx, connection, request, stream.Send)
	if ctx.Err() != nil {
		return rpcError(ctx.Err())
	}
	return err
}

type connectionKey struct{}

// connections numbers transports, for the hub's per-connection cap. gRPC derives
// every stream's context from the one TagConn returns.
type connections struct{ next atomic.Uint64 }

func (c *connections) TagConn(ctx context.Context, _ *stats.ConnTagInfo) context.Context {
	return context.WithValue(ctx, connectionKey{}, c.next.Add(1))
}

func (*connections) TagRPC(ctx context.Context, _ *stats.RPCTagInfo) context.Context { return ctx }
func (*connections) HandleRPC(context.Context, stats.RPCStats)                       {}
func (*connections) HandleConn(context.Context, stats.ConnStats)                     {}
