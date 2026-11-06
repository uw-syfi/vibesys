package main_test

import (
	"bytes"
	"context"
	"errors"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"
	"time"

	"google.golang.org/grpc"

	"github.com/uw-syfi/vibesys/gateway/internal/connect"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hostconfig"
)

// tempRoot is a root short enough for its socket to stay inside it.
func tempRoot(t *testing.T) string {
	t.Helper()
	root, err := os.MkdirTemp("", "h")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	real, err := filepath.EvalSymlinks(root)
	if err != nil {
		t.Fatal(err)
	}
	return real
}

// ask reaches the host of root with what any client has, the socket, and returns what it says it is.
func ask(ctx context.Context, t *testing.T, root string) *pb.IdentifyResponse {
	t.Helper()
	connection, err := awaitDial(ctx, lifecycle.SocketPath(root))
	if err != nil {
		t.Fatal(err)
	}
	connection.Close()
	id, err := connect.Identify(ctx, lifecycle.SocketPath(root), "")
	if err != nil {
		t.Fatal(err)
	}
	return id
}

// awaitDial connects, waiting only for a host that is coming up. ctx is the bound, and it guards only
// against a hang.
func awaitDial(ctx context.Context, socket string) (net.Conn, error) {
	for {
		connection, err := (&net.Dialer{}).DialContext(ctx, "unix", socket)
		if err == nil || ctx.Err() != nil {
			return connection, err
		}
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-time.After(10 * time.Millisecond):
		}
	}
}

// stop asks the host of root to stop, and waits for its lock to come free.
func stop(ctx context.Context, t *testing.T, root string) {
	t.Helper()
	if err := connect.StopIdle(ctx, lifecycle.SocketPath(root)); err != nil {
		t.Fatal(err)
	}
	awaitLockFree(ctx, t, root)
}

func awaitLockFree(ctx context.Context, t *testing.T, root string) {
	t.Helper()
	for {
		_, held, err := lifecycle.Holder(root)
		if err != nil {
			t.Fatal(err)
		}
		if !held {
			return
		}
		select {
		case <-ctx.Done():
			t.Fatalf("a process still holds the root %s", root)
		case <-time.After(10 * time.Millisecond):
		}
	}
}

// A host started from an environment that sets every old override to nonsense takes nothing from it:
// no part is the operator's choice, its state is where its root says, and nothing was made at any
// of the places the nonsense names.
func testHostileEnvironment(t *testing.T, binary string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	root := tempRoot(t)
	nonsense := filepath.Join(tempRoot(t), "nonsense")
	var env []string
	for _, kv := range os.Environ() {
		if !strings.HasPrefix(kv, "HOME=") {
			env = append(env, kv)
		}
	}
	for _, setting := range hostconfig.Retired {
		env = append(env, setting.Name+"="+filepath.Join(nonsense, setting.Name))
	}
	env = append(env, "VIBESYS_HOME="+filepath.Join(nonsense, "home"), "HOME="+tempRoot(t))
	command := exec.CommandContext(ctx, binary, "serve", "--root", root)
	command.Env = env
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	defer func() { command.Process.Kill(); command.Wait() }()
	id := ask(ctx, t, root)
	if id.GetServing() == nil {
		t.Fatalf("the host does not serve: %v\n%s", id, stderr.String())
	}
	if id.GetConfigDigest() != "" {
		t.Errorf("the host says it has a configuration: %q", id.GetConfigDigest())
	}
	for _, part := range id.GetParts() {
		if part.GetReady().GetOrigin() == pb.PartOrigin_PART_ORIGIN_OPERATOR || part.GetProblem().GetReason() == pb.PartProblemReason_PART_PROBLEM_REASON_OPERATOR_CHOICE_REFUSED {
			t.Errorf("part %v took a setting from the environment", part)
		}
	}
	if _, err := os.Stat(filepath.Join(root, "state", "agent.sock")); err != nil {
		t.Errorf("the state is not in the root: %v", err)
	}
	if _, err := os.Stat(nonsense); !errors.Is(err, os.ErrNotExist) {
		t.Errorf("a place the environment names was used: %v", err)
	}
	stop(ctx, t, root)
}

// A state directory nobody can use.
func refusedStateConfig(t *testing.T, root string) {
	t.Helper()
	blocker := filepath.Join(root, "a-file")
	if err := os.WriteFile(blocker, nil, 0o600); err != nil {
		t.Fatal(err)
	}
	if err := hostconfig.Save(root, &pb.HostConfig{StateDirectory: filepath.Join(blocker, "state")}); err != nil {
		t.Fatal(err)
	}
}

// A host whose state is refused ends, says why on stderr, and leaves no lock held and no socket.
func testRefusedStateLeavesNothing(t *testing.T, binary string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	root := tempRoot(t)
	refusedStateConfig(t, root)
	output, err := exec.CommandContext(ctx, binary, "serve", "--root", root).CombinedOutput()
	var exit *exec.ExitError
	if !errors.As(err, &exit) || exit.ExitCode() != 1 || !strings.Contains(string(output), "a-file") {
		t.Fatalf("serve = %v, %q; want exit 1 naming the state", err, output)
	}
	if _, held, err := lifecycle.Holder(root); err != nil || held {
		t.Fatalf("after the refusal: held %v, %v", held, err)
	}
	if _, err := os.Stat(lifecycle.SocketPath(root)); !errors.Is(err, os.ErrNotExist) {
		t.Fatalf("the refused host left a socket: %v", err)
	}
}

// The desktop app reads a host's logs whether or not the orchestrator can be served, so the host its
// bridge starts stays up, and says what is wrong when asked.
func testBridgeKeepsAHostWithRefusedState(t *testing.T, binary string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	root := tempRoot(t)
	refusedStateConfig(t, root)
	command := exec.CommandContext(ctx, binary, "connect", "--root", root)
	command.Stdin = bytes.NewReader(nil)
	if output, err := command.CombinedOutput(); err != nil {
		t.Fatalf("connect: %v\n%s", err, output)
	}
	id := ask(ctx, t, root)
	if id.GetUnserved().GetReason() != pb.UnservedReason_UNSERVED_REASON_STATE_REFUSED || id.GetApiSchemaHash() != schemahash.APISchemaHash {
		t.Fatalf("identity %v", id)
	}
	stop(ctx, t, root)
}

// otherAPIHost is a running host of another API hash: it answers only the control service.
type otherAPIHost struct {
	pb.UnimplementedHostControlServiceServer
	live     uint32
	endpoint *lifecycle.Endpoint
	server   *grpc.Server
	mu       sync.Mutex
	stops    []bool
}

func (h *otherAPIHost) Identify(context.Context, *pb.IdentifyRequest) (*pb.IdentifyResponse, error) {
	return &pb.IdentifyResponse{Release: "0.1.0", ApiSchemaHash: "another-hash", LiveRuns: h.live}, nil
}

func (h *otherAPIHost) Stop(_ context.Context, req *pb.StopRequest) (*pb.StopResponse, error) {
	h.mu.Lock()
	h.stops = append(h.stops, req.GetOnlyIfIdle())
	h.mu.Unlock()
	if req.GetOnlyIfIdle() && h.live > 0 {
		return &pb.StopResponse{Outcome: &pb.StopResponse_Refused{Refused: &pb.Busy{LiveRuns: h.live}}}, nil
	}
	go func() {
		h.server.Stop()
		h.endpoint.Close()
	}()
	return &pb.StopResponse{Outcome: &pb.StopResponse_Stopping{Stopping: &pb.Stopping{}}}, nil
}

func (h *otherAPIHost) stopped() []bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	return append([]bool(nil), h.stops...)
}

// The desktop app's bridge replaces an idle host of another API, only if idle, with its own. One with
// live runs is left as it is: nothing stops it, and the bridge says why on stderr.
func testBridgeReplacesAnotherAPI(t *testing.T, binary string) {
	for _, live := range []uint32{0, 1} {
		ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
		defer cancel()
		root := tempRoot(t)
		endpoint, err := lifecycle.Listen(root)
		if err != nil {
			t.Fatal(err)
		}
		host := &otherAPIHost{live: live, endpoint: endpoint, server: grpc.NewServer()}
		pb.RegisterHostControlServiceServer(host.server, host)
		go func() { _ = host.server.Serve(endpoint.Listener) }()
		command := exec.CommandContext(ctx, binary, "connect", "--root", root)
		command.Stdin = bytes.NewReader(nil)
		var stderr bytes.Buffer
		command.Stderr = &stderr
		if err := command.Run(); err != nil {
			t.Fatalf("%d live: connect: %v\n%s", live, err, stderr.String())
		}
		if live > 0 {
			if len(host.stopped()) != 0 || !strings.Contains(stderr.String(), "live") {
				t.Fatalf("%d live: stops %v, stderr %q", live, host.stopped(), stderr.String())
			}
			host.server.Stop()
			endpoint.Close()
			continue
		}
		if stops := host.stopped(); len(stops) != 1 || !stops[0] {
			t.Fatalf("idle: stops %v, want one, only if idle", stops)
		}
		if id := ask(ctx, t, root); id.GetApiSchemaHash() != schemahash.APISchemaHash {
			t.Fatalf("idle: the host now says %v", id)
		}
		stop(ctx, t, root)
	}
}
