package clientcli_test

import (
	"context"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// A run whose lead never opened because of a reason with a remedy says what to do, from the typed
// reason alone: the text of the lead's own end detail is not read.
func TestAFailedStartTeachesWhatToDoForEachReasonThatHasARemedy(t *testing.T) {
	cases := []struct {
		name   string
		reason pb.StartFailureReason
		config *pb.HostConfig
		want   []string
		notIn  []string
	}{
		{
			name: "not installed, default directories", reason: pb.StartFailureReason_START_FAILURE_REASON_AGENT_NOT_INSTALLED, config: &pb.HostConfig{},
			want: []string{"host configure --tool-path", "/home/u/.local/bin", "/usr/local/bin", "host restart"},
		},
		{
			name: "not installed, configured directories", reason: pb.StartFailureReason_START_FAILURE_REASON_AGENT_NOT_INSTALLED,
			config: &pb.HostConfig{ToolPath: []string{"/opt/tools/bin", "/srv/bin"}},
			want:   []string{"host configure --tool-path", "/opt/tools/bin, /srv/bin"}, notIn: []string{"/usr/local/bin"},
		},
		{
			name: "state location too long", reason: pb.StartFailureReason_START_FAILURE_REASON_STATE_LOCATION_TOO_LONG, config: &pb.HostConfig{},
			want: []string{"--root", "host configure --state-directory", "host restart"},
		},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_FAILED, 0)
			lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_FAILED)
			lead.EndDetail = "could not start the agent: words that are not read"
			lead.StartFailureReason = c.reason
			w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})}}))
			w.configs = &fakeConfigs{config: c.config}

			got := w.run(context.Background(), "run", "wait", "run-1", "--until", "ended", "--timeout", "90s")
			if got.code != 1 {
				t.Fatalf("exit %d, want 1; stderr %q", got.code, got.stderr)
			}
			contains(t, "stderr", got.stderr, c.want...)
			for _, absent := range c.notIn {
				if strings.Contains(got.stderr, absent) {
					t.Errorf("stderr names %q: %s", absent, got.stderr)
				}
			}
		})
	}

	// A reason with no remedy of ours adds nothing to the lead's own account.
	run := runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_FAILED, 0)
	lead := leadOf("run-1", pb.AgentStatus_AGENT_STATUS_FAILED)
	lead.EndDetail, lead.StartFailureReason = "could not start the agent: boom", pb.StartFailureReason_START_FAILURE_REASON_OTHER
	w := newWorld(t, serveFake(t, &fakeService{frames: []*pb.WatchRunsResponse{snapshotFrame(5, false, []*pb.Run{run}, []*pb.Agent{lead})}}))
	got := w.run(context.Background(), "run", "wait", "run-1", "--until", "ended", "--timeout", "90s")
	if strings.Contains(got.stderr, "to fix") {
		t.Errorf("advice for a reason with none: %s", got.stderr)
	}
}
