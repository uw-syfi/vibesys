package hosted_test

import (
	"context"
	"net"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/test/bufconn"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hosted"
)

// guard bounds a wait that only a hang would make long.
const guard = 30 * time.Second

type fakeHost struct {
	live     uint32
	unserved *pb.Unserved
}

func (f fakeHost) LiveRuns() uint32       { return f.live }
func (f fakeHost) Unserved() *pb.Unserved { return f.unserved }

type noParts struct{}

func (noParts) Readiness() []*pb.PartReadiness { return nil }

// control serves a HostControlService over a fake host and reports every call of its Stop.
func control(t *testing.T, host fakeHost) (pb.HostControlServiceClient, <-chan struct{}) {
	t.Helper()
	stopped := make(chan struct{}, 4)
	listener := bufconn.Listen(1 << 20)
	server := grpc.NewServer()
	pb.RegisterHostControlServiceServer(server, hosted.NewHostControl(hosted.ControlConfig{
		Release: "1.0.0", Started: time.Unix(1_700_000_000, 0), Parts: noParts{}, Host: host, Stop: func() { stopped <- struct{}{} },
	}))
	go func() { _ = server.Serve(listener) }()
	t.Cleanup(server.Stop)
	conn, err := grpc.NewClient("passthrough:///host", grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithContextDialer(func(ctx context.Context, _ string) (net.Conn, error) { return listener.DialContext(ctx) }))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { _ = conn.Close() })
	return pb.NewHostControlServiceClient(conn), stopped
}

// A host that is asked to stop only when idle refuses while any run is live, and says how many.
// Asked without that condition, it stops whatever is live, since the sessions outlive it.
func TestStopWithLiveRunsIsRefusedWhenAskedToStopOnlyIfIdle(t *testing.T) {
	for _, live := range []uint32{1, 2, 17} {
		client, stopped := control(t, fakeHost{live: live})
		got, err := client.Stop(context.Background(), &pb.StopRequest{OnlyIfIdle: true})
		if err != nil {
			t.Fatal(err)
		}
		if got.GetRefused().GetLiveRuns() != live || got.GetStopping() != nil {
			t.Fatalf("%d live runs: %v, want a refusal naming them", live, got)
		}
		select {
		case <-stopped:
			t.Fatalf("%d live runs: the host stopped although it refused", live)
		default:
		}
		if _, err := client.Stop(context.Background(), &pb.StopRequest{}); err != nil {
			t.Fatal(err)
		}
		select {
		case <-stopped:
		case <-time.After(guard):
			t.Fatalf("%d live runs: an unconditional stop did not stop the host", live)
		}
	}
}

func TestAnIdleHostStopsOnceAfterItsReply(t *testing.T) {
	client, stopped := control(t, fakeHost{})
	got, err := client.Stop(context.Background(), &pb.StopRequest{OnlyIfIdle: true})
	if err != nil || got.GetStopping() == nil {
		t.Fatalf("got %v, %v, want stopping", got, err)
	}
	select {
	case <-stopped:
	case <-time.After(guard):
		t.Fatal("the host did not stop")
	}
}

func TestIdentifySaysWhatTheHostIsWhetherOrNotItServes(t *testing.T) {
	for name, host := range map[string]fakeHost{
		"serving": {live: 3},
		"unserved": {unserved: &pb.Unserved{
			Reason: pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, HeldByRelease: "0.9.0",
		}},
	} {
		t.Run(name, func(t *testing.T) {
			client, _ := control(t, host)
			id, err := client.Identify(context.Background(), &pb.IdentifyRequest{ClientRelease: "1.1.0"})
			if err != nil {
				t.Fatal(err)
			}
			if id.GetRelease() != "1.0.0" || id.GetRelation() != pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER || id.GetLiveRuns() != host.live {
				t.Fatalf("identity %v", id)
			}
			if (id.GetServing() != nil) == (id.GetUnserved() != nil) || (host.unserved != nil) != (id.GetUnserved() != nil) {
				t.Fatalf("identity %v: exactly one of serving and unserved, matching the host", id)
			}
			if id.GetStartedAt().AsTime().Unix() != 1_700_000_000 {
				t.Fatalf("started at %v", id.GetStartedAt())
			}
		})
	}
}
