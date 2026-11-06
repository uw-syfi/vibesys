//go:build e2e

package e2e

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/encoding/protojson"

	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// dial opens the operator's OrchestratorService on the rig's gateway. The test observes the daemon
// through the typed stream itself, so what it asserts does not depend on how a client words it.
func (r *rig) dial(ctx context.Context) *grpc.ClientConn {
	r.t.Helper()
	connection, err := grpc.NewClient("passthrough:///gateway", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) {
		return (&net.Dialer{}).DialContext(ctx, "unix", lifecycle.SocketPath(r.root))
	}), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(4<<20)))
	if err != nil {
		r.t.Fatal(err)
	}
	return connection
}

// awaitServing returns once the gateway accepts connections. A gateway that was just started
// publishes its socket a moment before it listens, and the bound only guards against one that never does.
func (r *rig) awaitServing() {
	r.t.Helper()
	socket := lifecycle.SocketPath(r.root)
	deadline := time.Now().Add(bound)
	for {
		connection, err := net.Dial("unix", socket)
		if err == nil {
			connection.Close()
			return
		}
		if time.Now().After(deadline) {
			r.t.Fatalf("the gateway never accepted a connection on %s: %v\nserve log:\n%s", socket, err, r.serveLog())
		}
		time.Sleep(10 * time.Millisecond)
	}
}

// watch starts the stream of runs after a sequence (zero starts with a snapshot) and returns a
// reader of its frames, each as one line of JSON. The stream ends with ctx.
func (r *rig) watch(ctx context.Context, after uint64) func() (string, error) {
	r.t.Helper()
	connection := r.dial(ctx)
	context.AfterFunc(ctx, func() { connection.Close() })
	stream, err := pb.NewOrchestratorServiceClient(connection).WatchRuns(ctx, &pb.WatchRunsRequest{AfterSequence: after})
	if err != nil {
		r.t.Fatal(err)
	}
	return func() (string, error) {
		frame, err := stream.Recv()
		if err != nil {
			return "", err
		}
		raw, err := protojson.Marshal(frame)
		if err != nil {
			return "", err
		}
		var line bytes.Buffer
		if err := json.Compact(&line, raw); err != nil {
			return "", err
		}
		return line.String(), nil
	}
}
