package connect

import (
	"context"
	"errors"
	"fmt"
	"net"
	"sync"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// identifyBound is how long a host has to say what it is.
const identifyBound = 5 * time.Second

// control speaks HostControlService over one connection already made.
func control(connection net.Conn) (pb.HostControlServiceClient, func(), error) {
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
		connection.Close()
		return nil, nil, err
	}
	return pb.NewHostControlServiceClient(conn), func() { conn.Close(); connection.Close() }, nil
}

// Identify asks the host on socket what it is.
func Identify(ctx context.Context, socket, release string) (*pb.IdentifyResponse, error) {
	connection, err := dial(ctx, socket)
	if err != nil {
		return nil, err
	}
	service, closeIt, err := control(connection)
	if err != nil {
		return nil, err
	}
	defer closeIt()
	ctx, cancel := context.WithTimeout(ctx, identifyBound)
	defer cancel()
	return service.Identify(ctx, &pb.IdentifyRequest{ClientRelease: release})
}

// ErrStayed says a host that was asked to stop only if idle has live runs, so it stays.
var ErrStayed = errors.New("the host has live runs, so it stays")

// StopIdle asks the host on socket to stop if it has no live run, and waits until nobody serves the
// socket. A run that started since it last answered makes it refuse: no run is ever stopped to make
// room for a host.
func StopIdle(ctx context.Context, socket string) error {
	connection, err := dial(ctx, socket)
	if Absent(err) {
		return nil
	}
	if err != nil {
		return err
	}
	service, closeIt, err := control(connection)
	if err != nil {
		return err
	}
	reply, err := service.Stop(ctx, &pb.StopRequest{OnlyIfIdle: true})
	closeIt()
	switch {
	case err == nil && reply.GetRefused() != nil:
		return fmt.Errorf("%w: %d live", ErrStayed, reply.GetRefused().GetLiveRuns())
	case err != nil && status.Code(err) != codes.Unavailable && status.Code(err) != codes.Canceled:
		// The host may close the connection as it stops, before its reply is read.
		return err
	}
	tick := time.NewTicker(settleEvery)
	defer tick.Stop()
	for {
		connection, err := dial(ctx, socket)
		if Absent(err) {
			return nil
		}
		if err == nil {
			connection.Close()
		}
		select {
		case <-ctx.Done():
			return fmt.Errorf("the host still answers after being asked to stop: %w", ctx.Err())
		case <-tick.C:
		}
	}
}

// DialForAPI is Dial for a client of the host API, which shakes hands by the exact API hash and has
// nothing to do with a host of another. Such a host is replaced when it holds no live run. One with
// live runs stays, the client's handshake fails naming both hashes, and stayed is told why.
func DialForAPI(ctx context.Context, socket string, launcher Launcher, holding Holding, release, apiHash string, stayed func(id *pb.IdentifyResponse)) (net.Conn, error) {
	connection, err := Dial(ctx, socket, launcher, holding)
	if err != nil {
		return nil, err
	}
	// The connection the client will use is opened last, so that the answer is about its host.
	connection.Close()
	id, err := Identify(ctx, socket, release)
	if err != nil || id.GetApiSchemaHash() == apiHash {
		// A host that cannot say what it is leaves it to the handshake.
		return Dial(ctx, socket, launcher, holding)
	}
	if id.GetLiveRuns() > 0 {
		stayed(id)
		return Dial(ctx, socket, launcher, holding)
	}
	if err := StopIdle(ctx, socket); err != nil {
		if errors.Is(err, ErrStayed) {
			stayed(id)
			return Dial(ctx, socket, launcher, holding)
		}
		return nil, err
	}
	return Dial(ctx, socket, launcher, holding)
}
