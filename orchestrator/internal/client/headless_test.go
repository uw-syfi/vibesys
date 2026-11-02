package client_test

import (
	"bytes"
	"context"
	"fmt"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// The client and the daemon are built apart, so the versions they state must be one fact.
func TestClientAndDaemonStateTheSameProtocol(t *testing.T) {
	if client.ProtocolVersion != daemon.ProtocolVersion || client.MinProtocolVersion != daemon.MinProtocolVersion {
		t.Fatalf("client speaks %d (min %d), daemon %d (min %d)",
			client.ProtocolVersion, client.MinProtocolVersion, daemon.ProtocolVersion, daemon.MinProtocolVersion)
	}
}

type countIDs struct{ n int }

func (c *countIDs) NewCommandID() string { c.n++; return fmt.Sprintf("cli-%d", c.n) }

// receiptOf parses the last receipt line the headless client printed.
func receiptOf(t *testing.T, out *bytes.Buffer) *pb.Receipt {
	t.Helper()
	lines := strings.Split(strings.TrimSpace(out.String()), "\n")
	r := &pb.Receipt{}
	if err := protojson.Unmarshal([]byte(lines[len(lines)-1]), r); err != nil {
		t.Fatalf("not a receipt: %q: %v", lines[len(lines)-1], err)
	}
	return r
}

// The headless client starts, steers and stops a run through the public API alone, and says what
// the daemon answered, including a refusal.
func TestHeadlessStartsSendsAndStops(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 2*guard)
	defer cancel()
	rig := daemontest.NewRig(t)
	live := rig.Serve(t)
	c, err := client.Dial(ctx, live.Listener.Dial, client.Options{BinaryVersion: "test"})
	if err != nil {
		t.Fatal(err)
	}
	defer c.Close()
	var out bytes.Buffer
	h := client.Headless{Client: c, IDs: &countIDs{}, Out: &out}

	if err := h.StartRun(ctx, client.StartArgs{Repository: "/repo", Task: "task", Brief: "go", FanoutLimit: 1, Harness: "bogus"}); err == nil || !strings.Contains(err.Error(), "claude_code") {
		t.Fatalf("an unknown harness gave %v, want an error naming the choices", err)
	}
	if err := h.StartRun(ctx, client.StartArgs{Repository: "/repo", Task: "task", Brief: "go", FanoutLimit: 1, Harness: "claude_code"}); err != nil {
		t.Fatal(err)
	}
	run := receiptOf(t, &out).GetRunId()
	if run == "" {
		t.Fatalf("no run id in %q", out.String())
	}
	state, ok := rig.Daemon.State().Run(core.RunID(run))
	if !ok {
		t.Fatalf("the daemon has no run %s", run)
	}
	if err := h.Send(ctx, string(state.Lead), "look at the tests"); err != nil {
		t.Fatal(err)
	}
	if err := h.Send(ctx, "agent-nobody", "hello"); err == nil || !strings.Contains(err.Error(), "refused") {
		t.Fatalf("a message to a missing agent gave %v, want a refusal", err)
	}
	if err := h.StopRun(ctx, run); err != nil {
		t.Fatal(err)
	}
	if got, _ := rig.Daemon.State().Run(core.RunID(run)); got.Status.Final() == false && got.Status != core.RunStopping {
		t.Fatalf("after stop the run is %s", got.Status)
	}

	// Watching prints the snapshot, which names the run, and ends without an error when its
	// context does.
	wctx, stop := context.WithCancel(ctx)
	watched := &stopAfterWrite{stop: stop}
	if err := (client.Headless{Client: c, IDs: &countIDs{}, Out: watched}).Watch(wctx, 0); err != nil {
		t.Fatalf("watch ended with %v", err)
	}
	if !strings.Contains(watched.buf.String(), run) {
		t.Fatalf("the snapshot does not name run %s: %s", run, watched.buf.String())
	}
}

// stopAfterWrite ends a watch once it has printed something.
type stopAfterWrite struct {
	buf  bytes.Buffer
	stop context.CancelFunc
}

func (w *stopAfterWrite) Write(p []byte) (int, error) {
	defer w.stop()
	return w.buf.Write(p)
}
