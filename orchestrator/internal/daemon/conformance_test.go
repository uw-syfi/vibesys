package daemon_test

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// startScenario is a sidecar conformance scenario about a session that cannot start. The Python
// sidecar plays the same files (orchestrator/sidecar/tests/test_conformance.py); here the Fake
// plays what the file says a sidecar sends, and the daemon must record the reason it names.
type startScenario struct {
	Name     string                `json:"name"`
	Provider [][]map[string]string `json:"provider"`
	Expect   struct {
		StartFailure string `json:"start_failure"`
	} `json:"expect"`
}

func startScenarios(t *testing.T) []startScenario {
	t.Helper()
	files, err := filepath.Glob("../../sidecar/conformance/scenarios/*.json")
	if err != nil || len(files) == 0 {
		t.Fatalf("no conformance scenarios found: %v", err)
	}
	var out []startScenario
	for _, file := range files {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.Contains(string(data), `"start_failure_is_typed"`) {
			continue
		}
		var s startScenario
		if err := json.Unmarshal(data, &s); err != nil {
			t.Fatalf("%s: %v", file, err)
		}
		out = append(out, s)
	}
	if len(out) == 0 {
		t.Fatal("no scenario about a session that cannot start")
	}
	return out
}

func TestEveryStartFailureScenarioEndsTheLeadWithItsReason(t *testing.T) {
	for _, scenario := range startScenarios(t) {
		t.Run(scenario.Name, func(t *testing.T) {
			step := scenario.Provider[0][0]
			reason := pb.StartFailureReason(pb.StartFailureReason_value["START_FAILURE_REASON_"+step["start_fails"]])
			if reason == pb.StartFailureReason_START_FAILURE_REASON_UNSPECIFIED {
				t.Fatalf("scenario names reason %q the contract does not have", step["start_fails"])
			}
			if got := pb.StartFailureReason_name[int32(reason)]; got != scenario.Expect.StartFailure {
				t.Fatalf("scenario starts with %s but expects %s", got, scenario.Expect.StartFailure)
			}
			r := daemontest.NewRig(t)
			r.Sidecars.Opening = func(core.LaunchSession) []*pb.SidecarEvent {
				return failedToStart(reason, "could not start the agent: "+step["detail"])
			}
			receipt := dispatch(t, r, startRun("c1"))
			r.Settle(t)
			run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
			lead, _ := r.Daemon.State().Agent(run.Lead)
			if want := core.StartFailure(strings.ToLower(step["start_fails"])); lead.Status != core.AgentFailed || lead.StartFailure != want {
				t.Fatalf("lead %s with reason %q, want failed with %q", lead.Status, lead.StartFailure, want)
			}
			if lead.ConversationID != "" {
				t.Fatalf("lead was shown open: %+v", lead)
			}
		})
	}
}
