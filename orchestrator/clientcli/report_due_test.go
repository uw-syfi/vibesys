package clientcli_test

import (
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/clientcli/attachview"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// A request to the lead for a progress report is shown as a reminder sent to the lead, in words that
// say what it asked for, and not as the reminder to hand off that an unnamed reminder is read as.
func TestAReportRequestIsShownAsAReminderThatNamesTheReport(t *testing.T) {
	v := attachview.NewView("run-1")
	v.Load(&pb.RunsSnapshot{
		Runs: []*pb.Run{runOf("run-1", "/work/repo", pb.RunStatus_RUN_STATUS_RUNNING, 0)}, Agents: []*pb.Agent{leadOf("run-1", pb.AgentStatus_AGENT_STATUS_IDLE)},
		Sequence: 1,
	})
	request := &pb.Message{
		Id: "m1", RunId: "run-1", ToAgentId: "lead-run-1", Delivery: pb.Delivery_DELIVERY_QUEUED, At: at(60),
		Body: &pb.Message_ReportDue{ReportDue: &pb.ReportDue{EverySeconds: 3600, ReportsPosted: 2}},
	}
	changes := v.Apply(&pb.RunChange{Sequence: 2, RunId: "run-1", At: at(60), Body: &pb.RunChange_Message{Message: request}})
	if len(changes) != 1 || changes[0].Kind != attachview.ReminderSent {
		t.Fatalf("a report request made the changes %+v, want one reminder", changes)
	}
	got := v.Describe(changes[0], time.UTC)
	if want := "04:00:00  lead was asked for a progress report"; got != want {
		t.Fatalf("described as %q, want %q", got, want)
	}
}
