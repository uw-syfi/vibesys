//go:build e2e

package e2e

import (
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// A person or an agent can start a run, leave it, come back to check on it, and see it end, with
// nothing but vibesys-client: every verb here is the real binary against the real gateway, and the
// scripted agents run a toy task to a report. Nothing of the run is left once it has ended.
func TestAClientCanStartLeaveCheckAndStopARun(t *testing.T) {
	r := newRig(t)
	r.useCLI()
	r.actingAgents()
	repository := newToy(t, r.dir)
	r.serve()

	started := r.receipt("run", "start", "--repository", repository.path, "--task", toyTask, "--detach", "--allow-agent", "codex", "--json")
	if started.RunID == "" {
		t.Fatalf("run start printed no run: %+v", started)
	}
	// The run ends by itself once the lead has reported, so what its sessions recorded is read while they live.
	sidecars, groups := r.awaitReportRecordingProcesses(started.RunID)
	if out := r.client("run", "wait", started.RunID, "--until", "report", "--timeout", "80s"); !strings.Contains(out, "final report 1:") {
		t.Errorf("wait --until report printed %q", out)
	}

	var snapshot pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(r.client("run", "get", started.RunID, "--json")), &snapshot); err != nil {
		t.Fatal(err)
	}
	reported := false
	for _, m := range snapshot.GetMessages() {
		reported = reported || m.GetReport() != nil
	}
	if len(snapshot.GetRuns()) != 1 || snapshot.GetRuns()[0].GetId() != started.RunID || len(snapshot.GetAgents()) < 2 || !reported {
		t.Errorf("get --json after the report: want one run, its agents and its report, got %v", &snapshot)
	}

	// Nobody stops the run: its final report did.
	r.client("run", "wait", started.RunID, "--until", "ended", "--timeout", "80s")
	var ended pb.RunsSnapshot
	if err := protojson.Unmarshal([]byte(r.client("run", "get", started.RunID, "--json")), &ended); err != nil {
		t.Fatal(err)
	}
	if got := ended.GetRuns()[0]; got.GetStatus() != pb.RunStatus_RUN_STATUS_ENDED || got.GetStopReason() != pb.RunStopReason_RUN_STOP_REASON_FINISHED {
		t.Errorf("a final report left the run %s, stopped for %s; want ended, finished", got.GetStatus(), got.GetStopReason())
	}

	for _, pid := range untilNoneRunning(func() []int {
		var left []int
		for _, pid := range append(append([]int(nil), sidecars...), groups...) {
			if running(pid) {
				left = append(left, pid)
			}
		}
		return left
	}) {
		t.Errorf("process %d of the run is still running after it ended", pid)
	}
}
