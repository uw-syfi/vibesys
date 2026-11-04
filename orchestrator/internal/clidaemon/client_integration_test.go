package clidaemon_test

import (
	"context"
	"io/fs"
	"net"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/daemontest"
	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"google.golang.org/grpc"
	"google.golang.org/grpc/test/bufconn"
)

type memoryDialer struct{ listener *bufconn.Listener }

func (d memoryDialer) Dial(ctx context.Context, _ clidaemon.Socket) (net.Conn, error) {
	return d.listener.DialContext(ctx)
}

func TestClientAgainstDaemon(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	clidaemontest.ContractWithContext(t, ctx, func(t *testing.T) clidaemon.Service {
		server := daemontest.Server(t)
		listener := bufconn.Listen(1 << 20)
		grpcServer := grpc.NewServer()
		pb.RegisterDaemonServiceServer(grpcServer, server)
		go func() { _ = grpcServer.Serve(listener) }()
		t.Cleanup(func() { grpcServer.Stop(); listener.Close() })
		socket, err := clidaemon.Locate(clidaemon.Location{}, 7, &fakeFS{info: clidaemon.Directory{Mode: 0700 | fs.ModeDir, UID: 7}})
		if err != nil {
			t.Fatal(err)
		}
		client, err := clidaemon.NewClient(socket, memoryDialer{listener})
		if err != nil {
			t.Fatal(err)
		}
		t.Cleanup(func() { client.Close() })
		return client
	})
}
