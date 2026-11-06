package clientcli_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// Whether the lead opened is read from its status and why it ended, whatever its session can do: a
// session that can neither steer nor resume has no capabilities to show, and has still opened.
func TestStartedReadsTheLeadsStatusAndNotItsCapabilities(t *testing.T) {
	const never = pb.StartFailureReason_START_FAILURE_REASON_NOT_SIGNED_IN
	cases := []struct {
		status pb.AgentStatus
		reason pb.StartFailureReason
		want   bool
	}{
		{status: pb.AgentStatus_AGENT_STATUS_LAUNCHING},
		{status: pb.AgentStatus_AGENT_STATUS_ACTIVE, want: true},
		{status: pb.AgentStatus_AGENT_STATUS_WAITING, want: true},
		{status: pb.AgentStatus_AGENT_STATUS_IDLE, want: true},
		{status: pb.AgentStatus_AGENT_STATUS_ENDED, want: true},
		{status: pb.AgentStatus_AGENT_STATUS_FAILED, want: true},
		{status: pb.AgentStatus_AGENT_STATUS_LOST, want: true},
		{status: pb.AgentStatus_AGENT_STATUS_FAILED, reason: never},
		{status: pb.AgentStatus_AGENT_STATUS_LOST, reason: never},
	}
	for _, capabilities := range []*pb.Capabilities{nil, {Steers: true, Resumes: true}} {
		for _, c := range cases {
			lead := leadOf("run-1", c.status)
			lead.Capabilities, lead.StartFailureReason = capabilities, c.reason
			v := clientcli.NewView("run-1")
			v.Load(&pb.RunsSnapshot{Sequence: 1, Runs: []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, Agents: []*pb.Agent{lead}})
			if got := v.Started(); got != c.want {
				t.Errorf("a lead %v with reason %v and capabilities %v: Started is %v, want %v", c.status, c.reason, capabilities, got, c.want)
			}
		}
	}
	if clientcli.NewView("run-1").Started() {
		t.Error("a view with no lead yet says it started")
	}
}
