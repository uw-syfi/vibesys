package clientcli_test

import (
	"bytes"
	"context"
	"encoding/json"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/grpc/test/bufconn"
	"google.golang.org/protobuf/types/known/timestamppb"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// fakeHost is a host process: it answers HostControlService while it runs, and a stop that is
// accepted ends it. Nothing here starts one except Connect, as the real client does.
type fakeHost struct {
	pb.UnimplementedHostControlServiceServer
	mu       sync.Mutex
	running  bool
	identity *pb.IdentifyResponse
	// oldHost makes it answer as a host that predates host control.
	oldHost bool
	stops   []bool
	starts  int
}

func (h *fakeHost) isRunning() bool { h.mu.Lock(); defer h.mu.Unlock(); return h.running }

func (h *fakeHost) Identify(context.Context, *pb.IdentifyRequest) (*pb.IdentifyResponse, error) {
	if h.oldHost {
		return nil, status.Error(codes.Unimplemented, "unknown service")
	}
	return h.identity, nil
}

func (h *fakeHost) Stop(_ context.Context, req *pb.StopRequest) (*pb.StopResponse, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if h.oldHost {
		return nil, status.Error(codes.Unimplemented, "unknown service")
	}
	h.stops = append(h.stops, req.GetOnlyIfIdle())
	if req.GetOnlyIfIdle() && h.identity.GetLiveRuns() > 0 {
		return &pb.StopResponse{Outcome: &pb.StopResponse_Refused{Refused: &pb.Busy{LiveRuns: h.identity.GetLiveRuns()}}}, nil
	}
	h.running = false
	return &pb.StopResponse{Outcome: &pb.StopResponse_Stopping{Stopping: &pb.Stopping{}}}, nil
}

// dial connects to the host, or fails as a socket nobody serves does.
func (h *fakeHost) dial(ctx context.Context) (net.Conn, error) {
	h.mu.Lock()
	running := h.running
	h.mu.Unlock()
	if !running {
		return nil, clientcli.ErrNoHost
	}
	listener := bufconn.Listen(1 << 16)
	server := grpc.NewServer()
	pb.RegisterHostControlServiceServer(server, h)
	go func() { _ = server.Serve(listener) }()
	conn, err := listener.DialContext(ctx)
	if err != nil {
		return nil, err
	}
	return &stoppingConn{Conn: conn, stop: server.Stop}, nil
}

// stoppingConn stops the in-memory server when the client closes the connection.
type stoppingConn struct {
	net.Conn
	stop func()
}

func (c *stoppingConn) Close() error { err := c.Conn.Close(); c.stop(); return err }

// pacedClock fires a short timer at once and a long one never: the verbs' polls run through at
// once, and the bound on one call to the host never ends it.
type pacedClock struct{}

func (pacedClock) Now() time.Time { return time.Unix(1_700_000_000, 0) }
func (pacedClock) After(d time.Duration) <-chan time.Time {
	ch := make(chan time.Time, 1)
	if d <= time.Second {
		ch <- pacedClock{}.Now()
	}
	return ch
}

type fakeProcesses struct {
	holder     *pb.HostLock
	terminated []*pb.HostLock
}

func (f *fakeProcesses) Holder(string) (*pb.HostLock, error) { return f.holder, nil }
func (f *fakeProcesses) Terminate(_ context.Context, _ string, h *pb.HostLock) error {
	f.terminated = append(f.terminated, h)
	return nil
}

func hostWorld(h *fakeHost, procs clientcli.HostProcesses) clientcli.Env {
	return clientcli.Env{
		Version: "1.0.0", Dir: "/", Zone: time.UTC, Clock: pacedClock{}, FS: nil,
		Connect: func(string) (clientcli.Dial, error) {
			return func(ctx context.Context) (net.Conn, error) {
				h.mu.Lock()
				if !h.running {
					h.running = true
					h.starts++
				}
				h.mu.Unlock()
				return h.dial(ctx)
			}, nil
		},
		Locate: func(string) (clientcli.Dial, error) { return h.dial, nil },
		Hosts:  procs,
	}
}

func runHost(env clientcli.Env, args ...string) (code int, stdout, stderr string) {
	var out, errb bytes.Buffer
	env.Stdout, env.Stderr = &out, &errb
	code = clientcli.Main(context.Background(), args, env)
	return code, out.String(), errb.String()
}

func identity(live uint32) *pb.IdentifyResponse {
	return &pb.IdentifyResponse{
		Release: "1.0.0", Relation: pb.ReleaseRelation_RELEASE_RELATION_SAME, StartedAt: timestamppb.New(time.Unix(1_700_000_000, 0)),
		LiveRuns: live,
		Parts: []*pb.PartReadiness{
			{Part: pb.HostPart_HOST_PART_SESSION_HOLDER, State: &pb.PartReadiness_Ready{Ready: &pb.PartReady{Origin: pb.PartOrigin_PART_ORIGIN_RELEASE}}},
			{Part: pb.HostPart_HOST_PART_JOB_TOOL, State: &pb.PartReadiness_Problem{Problem: &pb.PartProblem{Reason: pb.PartProblemReason_PART_PROBLEM_REASON_RELEASE_INCOMPLETE, Detail: "missing bin/vibesys-cli"}}},
		},
		Orchestrator: &pb.IdentifyResponse_Serving{Serving: &pb.Serving{}},
	}
}

func TestHostStatusSaysWhatTheHostIsAndNeverStartsOne(t *testing.T) {
	h := &fakeHost{running: true, identity: identity(2)}
	code, out, errb := runHost(hostWorld(h, nil), "host", "status")
	if code != clientcli.ExitOK {
		t.Fatalf("exit %d: %s", code, errb)
	}
	for _, want := range []string{"release 1.0.0", "the same as this client", "live runs: 2", "orchestrator: serving", "session holder: ready, from the release", "job tool: cannot be used: missing bin/vibesys-cli"} {
		if !strings.Contains(out, want) {
			t.Errorf("status lacks %q:\n%s", want, out)
		}
	}
	code, out, _ = runHost(hostWorld(h, nil), "host", "status", "--json")
	var parsed map[string]any
	if code != clientcli.ExitOK || json.Unmarshal([]byte(out), &parsed) != nil || parsed["release"] != "1.0.0" || parsed["liveRuns"] != float64(2) {
		t.Fatalf("--json: exit %d, %s", code, out)
	}

	gone := &fakeHost{}
	code, _, errb = runHost(hostWorld(gone, nil), "host", "status")
	if code != clientcli.ExitUnavailable || !strings.Contains(errb, "no host is running") || gone.starts != 0 {
		t.Fatalf("no host: exit %d, %q, started %d", code, errb, gone.starts)
	}
}

func TestHostStatusSaysWhyTheOrchestratorIsNotServed(t *testing.T) {
	id := identity(0)
	id.Orchestrator = &pb.IdentifyResponse_Unserved{Unserved: &pb.Unserved{Reason: pb.UnservedReason_UNSERVED_REASON_HELD_ELSEWHERE, HeldByRelease: "0.9.0", Detail: "another host"}}
	id.Relation = pb.ReleaseRelation_RELEASE_RELATION_HOST_OLDER
	code, out, _ := runHost(hostWorld(&fakeHost{running: true, identity: id}, nil), "host", "status")
	if code != clientcli.ExitOK || !strings.Contains(out, "held by another host, release 0.9.0") || !strings.Contains(out, "older than this client") {
		t.Fatalf("exit %d:\n%s", code, out)
	}
}

// Whatever the number of live runs, a stop that must find none is refused exactly when there are
// some, and says how many; the host stays up. Without that condition it stops either way.
func TestHostStopIfNoLiveRunsRefusesExactlyWhenRunsAreLive(t *testing.T) {
	for live := uint32(0); live <= 5; live++ {
		h := &fakeHost{running: true, identity: identity(live)}
		code, out, errb := runHost(hostWorld(h, nil), "host", "stop", "--if-no-live-runs")
		if live > 0 {
			if code != clientcli.ExitFailed || !h.isRunning() || !strings.Contains(errb, "not stopping") || !strings.Contains(errb, string(rune('0'+live))) {
				t.Errorf("%d live: exit %d, running %v, stderr %q", live, code, h.isRunning(), errb)
			}
		} else if code != clientcli.ExitOK || h.isRunning() || !strings.Contains(out, "stopped") {
			t.Errorf("idle: exit %d, running %v, stdout %q, stderr %q", code, h.isRunning(), out, errb)
		}
		if h.starts != 0 {
			t.Errorf("%d live: a stop started a host", live)
		}
		if live > 0 {
			if code, _, _ := runHost(hostWorld(h, nil), "host", "stop"); code != clientcli.ExitOK || h.isRunning() {
				t.Errorf("%d live: a plain stop should stop the host, exit %d", live, code)
			}
		}
	}
}

func TestHostStopWithNoHostIsOkAndStartsNothing(t *testing.T) {
	h := &fakeHost{}
	code, out, errb := runHost(hostWorld(h, nil), "host", "stop", "--if-no-live-runs")
	if code != clientcli.ExitOK || h.starts != 0 || !strings.Contains(out, "no host is running") {
		t.Fatalf("exit %d, started %d, %q %q", code, h.starts, out, errb)
	}
}

// A host that predates host control cannot be asked: stop says so with exit 4. With --force the
// process its lock names is ended, and nothing is ended when the lock is free.
func TestHostStopForceEndsTheProcessOfAHostThatDoesNotAnswer(t *testing.T) {
	old := &fakeHost{running: true, oldHost: true, identity: identity(0)}
	code, _, errb := runHost(hostWorld(old, nil), "host", "stop")
	if code != clientcli.ExitUnavailable || !strings.Contains(errb, "predates host control") {
		t.Fatalf("exit %d: %s", code, errb)
	}

	holder := &pb.HostLock{Pid: 4242, Release: "0.1.0"}
	procs := &fakeProcesses{holder: holder}
	code, out, errb := runHost(hostWorld(old, procs), "host", "stop", "--force")
	if code != clientcli.ExitOK || len(procs.terminated) != 1 || procs.terminated[0].GetPid() != 4242 || !strings.Contains(out, "4242") {
		t.Fatalf("exit %d, ended %v, %q %q", code, procs.terminated, out, errb)
	}

	free := &fakeProcesses{}
	code, out, _ = runHost(hostWorld(old, free), "host", "stop", "--force")
	if code != clientcli.ExitOK || len(free.terminated) != 0 || !strings.Contains(out, "no host holds the lock") {
		t.Fatalf("a free lock: exit %d, ended %v, %q", code, free.terminated, out)
	}

	// A host that answers is stopped through its service, and --force leaves its process alone.
	good := &fakeHost{running: true, identity: identity(0)}
	forced := &fakeProcesses{holder: holder}
	if code, _, _ := runHost(hostWorld(good, forced), "host", "stop", "--force"); code != clientcli.ExitOK || len(forced.terminated) != 0 || good.isRunning() {
		t.Fatalf("exit %d, ended %v, running %v", code, forced.terminated, good.isRunning())
	}
}

func TestHostRestartIsRefusedWithLiveRunsAndOtherwiseReplacesTheHost(t *testing.T) {
	busy := &fakeHost{running: true, identity: identity(1)}
	code, _, errb := runHost(hostWorld(busy, nil), "host", "restart")
	if code != clientcli.ExitFailed || !strings.Contains(errb, "not restarting") || !busy.isRunning() || busy.starts != 0 || len(busy.stops) != 0 {
		t.Fatalf("live runs: exit %d, running %v, started %d, stops %v: %s", code, busy.isRunning(), busy.starts, busy.stops, errb)
	}

	idle := &fakeHost{running: true, identity: identity(0)}
	code, out, errb := runHost(hostWorld(idle, nil), "host", "restart")
	if code != clientcli.ExitOK || idle.starts != 1 || !idle.isRunning() || len(idle.stops) != 1 || !idle.stops[0] || !strings.Contains(out, "release 1.0.0") {
		t.Fatalf("idle: exit %d, started %d, stops %v, running %v, %q %q", code, idle.starts, idle.stops, idle.isRunning(), out, errb)
	}

	none := &fakeHost{identity: identity(0)}
	code, _, errb = runHost(hostWorld(none, nil), "host", "restart")
	if code != clientcli.ExitOK || none.starts != 1 || len(none.stops) != 0 {
		t.Fatalf("no host: exit %d, started %d, stops %v: %s", code, none.starts, none.stops, errb)
	}
}

func TestHostVerbsRefuseWhatTheyDoNotTake(t *testing.T) {
	h := &fakeHost{running: true, identity: identity(0)}
	for _, args := range [][]string{{"host"}, {"host", "dance"}, {"host", "status", "x"}, {"host", "stop", "--nope"}} {
		if code, _, _ := runHost(hostWorld(h, nil), args...); code != clientcli.ExitUsage {
			t.Errorf("%v: exit %d, want a usage error", args, code)
		}
	}
}
