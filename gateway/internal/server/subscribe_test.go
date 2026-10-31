package server_test

import (
	"context"
	"errors"
	"io"
	"net"
	"reflect"
	"runtime"
	"slices"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/clock"
	"github.com/uw-syfi/vibesys/gateway/internal/hostoperations"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats/machinestatstest"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription/subscriptiontest"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/reflect/protoreflect"
)

var machineStats = &apiv1.SubscribeRequest{Topic: &apiv1.SubscribeRequest_MachineStats{MachineStats: &apiv1.MachineStatsTopic{}}}

// listener hands the server connections from memory, so the whole gRPC stack
// runs inside a synctest bubble: keepalive timers included, on fake time.
type listener struct {
	accepted chan net.Conn
	closed   chan struct{}
}

func (l *listener) Accept() (net.Conn, error) {
	select {
	case connection := <-l.accepted:
		return connection, nil
	case <-l.closed:
		return nil, net.ErrClosed
	}
}

func (l *listener) Close() error {
	select {
	case <-l.closed:
	default:
		close(l.closed)
	}
	return nil
}

func (l *listener) Addr() net.Addr { return &net.UnixAddr{Name: "memory", Net: "unix"} }

// link joins a client and a server. Once cut, it swallows everything in both
// directions without closing either end: a half-open connection, where writes
// keep succeeding into a socket buffer and nothing ever comes back.
type link struct{ cut atomic.Bool }

func (l *link) forward(from, to net.Conn) {
	defer to.Close()
	buffer := make([]byte, 32<<10)
	for {
		size, err := from.Read(buffer)
		if err != nil {
			return
		}
		if l.cut.Load() {
			continue
		}
		if _, err := to.Write(buffer[:size]); err != nil {
			return
		}
	}
}

type gateway struct {
	t        *testing.T
	listener *listener
	links    []*link
}

// serve runs a server inside the bubble until the test ends.
func serve(t *testing.T, host machinestats.Host) *gateway {
	t.Helper()
	ctx, cancel := context.WithCancel(context.Background())
	running := &gateway{t: t, listener: &listener{accepted: make(chan net.Conn), closed: make(chan struct{})}}
	done := make(chan error, 1)
	go func() {
		done <- server.Serve(ctx, running.listener, server.Config{Root: t.TempDir(), Version: "test-version", Machine: host})
	}()
	t.Cleanup(func() {
		cancel()
		running.listener.Close()
		if err := <-done; err != nil {
			t.Error(err)
		}
		synctest.Wait()
	})
	return running
}

// connect opens one transport. Its link is the last in links.
func (g *gateway) connect() apiv1.HostServiceClient {
	g.t.Helper()
	wire := &link{}
	g.links = append(g.links, wire)
	dial := func(context.Context, string) (net.Conn, error) {
		clientEnd, clientWire := net.Pipe()
		serverWire, serverEnd := net.Pipe()
		go wire.forward(clientWire, serverWire)
		go wire.forward(serverWire, clientWire)
		select {
		case g.listener.accepted <- serverEnd:
		case <-g.listener.closed:
			return nil, net.ErrClosed
		}
		return clientEnd, nil
	}
	connection, err := grpc.NewClient("passthrough:///gateway", grpc.WithContextDialer(dial), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		g.t.Fatal(err)
	}
	g.t.Cleanup(func() { connection.Close() })
	return apiv1.NewHostServiceClient(connection)
}

// receiver records a stream's frames for the test's main goroutine.
type receiver struct {
	frames atomic.Pointer[[]*apiv1.SubscribeResponse]
	ended  atomic.Pointer[error]
}

func subscribe(ctx context.Context, t *testing.T, client apiv1.HostServiceClient) *receiver {
	t.Helper()
	stream, err := client.Subscribe(ctx, machineStats)
	if err != nil {
		t.Fatal(err)
	}
	received := &receiver{}
	go func() {
		var frames []*apiv1.SubscribeResponse
		for {
			frame, err := stream.Recv()
			if err != nil {
				received.ended.Store(&err)
				return
			}
			frames = append(frames, frame)
			published := slices.Clone(frames)
			received.frames.Store(&published)
		}
	}()
	return received
}

func (r *receiver) seen() []*apiv1.SubscribeResponse {
	if frames := r.frames.Load(); frames != nil {
		return *frames
	}
	return nil
}

func TestSubscribeServesAMachineSnapshotOverGRPC(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		running := serve(t, machinestatstest.Recorded(runtime.GOOS))
		received := subscribe(ctx, t, running.connect())
		time.Sleep(hostoperations.Topics[hostoperations.TopicMachineStats].FirstFrame)
		synctest.Wait()
		frames := received.seen()
		if len(frames) < 2 || frames[0].GetEntry().GetMachineStats() == nil || frames[0].GetEntry().Key != machinestats.Key {
			t.Fatalf("frames %v", frames)
		}
		if !slices.ContainsFunc(frames, func(frame *apiv1.SubscribeResponse) bool { return frame.GetSynced() != nil }) {
			t.Fatalf("no synced in %v", frames)
		}
		cancel()
		synctest.Wait()
	})
}

func TestTheSixtyFifthStreamOnAConnectionIsRefusedAndAnotherConnectionIsNot(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		running := serve(t, machinestatstest.Recorded(runtime.GOOS))
		client := running.connect()
		for range subscription.MaxPerConnection {
			subscribe(ctx, t, client)
		}
		synctest.Wait()
		refused := subscribe(ctx, t, client)
		synctest.Wait()
		frames := refused.seen()
		if len(frames) != 1 || frames[0].GetFailed().GetCode() != apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_RESOURCE_EXHAUSTED {
			t.Fatalf("frames %v", frames)
		}
		if end := refused.ended.Load(); end == nil || !errors.Is(*end, io.EOF) {
			t.Fatalf("a refusal must end the stream cleanly, ended %v", end)
		}
		other := subscribe(ctx, t, running.connect())
		synctest.Wait()
		if end := other.ended.Load(); end != nil {
			t.Fatalf("second connection ended: %v", *end)
		}
		cancel()
		synctest.Wait()
	})
}

// The gateway's half of a half-open connection: its writes keep succeeding and
// nothing comes back. Only keepalive can end the stream, and with it the producer.
func TestAHalfOpenConnectionStopsItsProducerAtTheKeepaliveBound(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		ctx, cancel := context.WithCancel(context.Background())
		defer cancel()
		host := machinestatstest.Recorded(runtime.GOOS)
		running := serve(t, host)
		subscribe(ctx, t, running.connect())
		time.Sleep(hostoperations.Topics[hostoperations.TopicMachineStats].FirstFrame)
		synctest.Wait()

		running.links[0].cut.Store(true)
		before := host.Calls.Load()
		time.Sleep(server.KeepaliveTime - time.Second)
		synctest.Wait()
		if host.Calls.Load() == before {
			t.Fatal("the producer stopped before the keepalive bound")
		}
		time.Sleep(server.KeepaliveTimeout + 2*time.Second)
		synctest.Wait()
		// One sample may still be in flight at the bound. Its section reads end with it.
		time.Sleep(machinestats.SampleDeadline)
		synctest.Wait()
		stopped := host.Calls.Load()
		time.Sleep(10 * hostoperations.Topics[hostoperations.TopicMachineStats].Cadence)
		synctest.Wait()
		if after := host.Calls.Load(); after != stopped {
			t.Fatalf("the producer ran on with no client: %d more host calls", after-stopped)
		}
		cancel()
		synctest.Wait()
	})
}

func TestKeepaliveBoundsAreTheDesignedOnes(t *testing.T) {
	if server.KeepaliveTime != 30*time.Second || server.KeepaliveTimeout != 10*time.Second {
		t.Fatalf("keepalive %s then %s", server.KeepaliveTime, server.KeepaliveTimeout)
	}
}

func oneofArms(message protoreflect.ProtoMessage, oneof protoreflect.Name) []string {
	fields := message.ProtoReflect().Descriptor().Oneofs().ByName(oneof).Fields()
	var names []string
	for index := range fields.Len() {
		names = append(names, string(fields.Get(index).Name()))
	}
	slices.Sort(names)
	return names
}

// contractCases holds one contract per topic. A generated topic missing here
// fails TestEveryGeneratedTopicPassesTheContract.
var contractCases = map[string]func(*testing.T){
	hostoperations.TopicMachineStats: func(t *testing.T) {
		for _, goos := range []string{"linux", "darwin"} {
			t.Run(goos, func(t *testing.T) {
				subscriptiontest.Contract(t, func() subscriptiontest.Subject[*apiv1.MachineStats] {
					return subscriptiontest.Subject[*apiv1.MachineStats]{
						Topic:   machinestats.Topic(goos, clock.System{}, machinestatstest.Recorded(goos)),
						Request: machineStats,
						Failing: machinestats.Topic("plan9", clock.System{}, machinestatstest.Linux()).Source,
						Failure: apiv1.TopicFailureCode_TOPIC_FAILURE_CODE_UNSUPPORTED,
					}
				})
			})
		}
	},
}

func TestEveryGeneratedTopicPassesTheContract(t *testing.T) {
	requested := oneofArms(&apiv1.SubscribeRequest{}, "topic")
	valued := oneofArms(&apiv1.TopicEntry{}, "value")
	if !slices.Equal(requested, valued) {
		t.Fatalf("request arms %v, value arms %v", requested, valued)
	}
	if len(hostoperations.Topics) != len(requested) {
		t.Fatalf("generated topics %v, request arms %v", hostoperations.Topics, requested)
	}
	for _, name := range requested {
		if _, generated := hostoperations.Topics[name]; !generated {
			t.Fatalf("topic %s is not generated", name)
		}
		run, ok := contractCases[name]
		if !ok {
			t.Fatalf("topic %s has no contract case", name)
		}
		t.Run(name, run)
	}
}

// Live state belongs on Subscribe. A new stream here is a new set of lifetime
// rules, so adding one must argue, in this table, why it cannot be a topic.
func TestStreamingMethodsAreTheAllowedOnes(t *testing.T) {
	allowed := map[string]string{
		"Subscribe":  "the live-state primitive itself",
		"Tail":       "every record matters, so values cannot conflate",
		"TailStream": "a byte stream, where a skipped byte corrupts the output",
		"Watch":      "a Python operation's progress stream, ending with the operation",
		"WatchRuns":  "remove in PR4: the run list moves onto Subscribe",
	}
	methods := apiv1.File_vibesys_api_v1_host_proto.Services().ByName("HostService").Methods()
	found := map[string]bool{}
	for index := range methods.Len() {
		if method := methods.Get(index); method.IsStreamingServer() || method.IsStreamingClient() {
			found[string(method.Name())] = true
			if allowed[string(method.Name())] == "" {
				t.Errorf("streaming method %s must be a topic, or say here why it cannot be", method.Name())
			}
		}
	}
	for name := range allowed {
		if !found[name] {
			t.Errorf("%s is allowed but no longer streams: drop its entry", name)
		}
	}
}

// Frames are uniform by construction. A new arm changes every client's fold and
// the frame corpus, so it is a deliberate change here.
func TestFrameShapesArePinned(t *testing.T) {
	frames := oneofArms(&apiv1.SubscribeResponse{}, "frame")
	if want := []string{"entry", "failed", "heartbeat", "removed", "synced"}; !slices.Equal(frames, want) {
		t.Fatalf("frame arms %v", frames)
	}
	fields := reflect.VisibleFields(reflect.TypeFor[hostoperations.TopicPolicy]())
	policy := (&apiv1.TopicPolicy{}).ProtoReflect().Descriptor().Fields()
	if len(fields) != policy.Len() {
		t.Fatalf("Go policy has %d fields, the proto %d: the generator drops one", len(fields), policy.Len())
	}
}
