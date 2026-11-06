package daemon_test

import (
	"bytes"
	"testing"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

// What the host keeps for itself is there and reaches no client: not in a snapshot, not in the changes
// replayed to a client that resumes or sent live to one that stays, and not in an answer to a command.
func TestNoPrivateFactReachesAClient(t *testing.T) {
	r := daemontest.NewRig(t)
	r.JobTool = toolA()
	r.Restart(t)
	live, first, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer live.Close()
	frames := []proto.Message{}
	for _, m := range first {
		frames = append(frames, m)
	}

	start := startRun("start")
	frames = append(frames, dispatch(t, r, start))
	r.Settle(t)
	run := r.Daemon.State().Runs()[0]
	lead, _ := r.Daemon.State().Agent(run.Lead)
	command, err := wire.ParseCommandMessage(start, core.Operator{})
	if err != nil {
		t.Fatal(err)
	}
	private := map[string]string{
		"job tool executable": run.JobTool.Executable,
		"job tool directory":  run.JobTool.RuntimeDirectory,
		"resume token":        lead.ResumeToken,
		"command digest":      string(command.Digest),
	}
	for name, value := range private {
		if value == "" {
			t.Fatalf("the %s is empty, so finding it nowhere would prove nothing", name)
		}
	}

	polled, err := live.Poll()
	if err != nil {
		t.Fatal(err)
	}
	replay, replayed, err := r.Service.WatchRunsFrom(core.Operator{}, 1)
	if err != nil {
		t.Fatal(err)
	}
	defer replay.Close()
	fresh, fromSnapshot, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer fresh.Close()
	if len(polled) == 0 || len(replayed) == 0 || len(fromSnapshot) == 0 {
		t.Fatalf("%d live, %d replayed and %d snapshot frames: nothing to check", len(polled), len(replayed), len(fromSnapshot))
	}
	for _, list := range [][]*pb.WatchRunsResponse{polled, replayed, fromSnapshot} {
		for _, m := range list {
			frames = append(frames, m)
		}
	}
	for _, frame := range frames {
		data, err := proto.Marshal(frame)
		if err != nil {
			t.Fatal(err)
		}
		for name, value := range private {
			if bytes.Contains(data, []byte(value)) {
				t.Errorf("a client was sent the %s: %v", name, frame)
			}
		}
	}
}
