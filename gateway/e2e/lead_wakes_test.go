//go:build e2e

package e2e

import (
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// minJobNotices is how many job-completion notices must have woken the lead: the two baseline jobs and
// the four workers' jobs finish before the lead's last turn. The two jobs after the promotion are
// awaited by the lead itself, and the run ends the moment it has reported: a job that finishes after a
// finished run's report wakes nobody, so those notices may not come. The fixture submits eight jobs.
const (
	minJobNotices = 6
	maxJobNotices = 8
)

// awaitLeadDeliveries waits until the run is over, then reads the typed mailbox of its snapshot as
// `run get` serves it (it holds the messages of a run that is over) and returns how many job notices
// woke the lead. Every message to the lead must have been consumed: the run ends only when nothing is in
// transit, so a message that is not consumed was lost. The bound only guards against a run that never ends.
func (r *rig) awaitLeadDeliveries(run, lead string) (jobNotices int) {
	r.t.Helper()
	deadline := time.Now().Add(bound)
	for {
		var snapshot pb.RunsSnapshot
		if err := protojson.Unmarshal([]byte(r.client("run", "get", run, "--json")), &snapshot); err != nil {
			r.t.Fatal(err)
		}
		if len(snapshot.GetRuns()) == 1 && snapshot.GetRuns()[0].GetStatus() == pb.RunStatus_RUN_STATUS_ENDED {
			return leadDeliveries(r.t, snapshot.GetMessages(), lead)
		}
		if time.Now().After(deadline) {
			r.t.Fatalf("the run was not over after %s", bound)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

// leadDeliveries checks what woke the lead: one brief, three handoffs, and between minJobNotices and
// maxJobNotices job notices, each job at most once, all consumed. It returns the number of job notices.
func leadDeliveries(t *testing.T, messages []*pb.Message, lead string) int {
	t.Helper()
	briefs, handoffs := 0, 0
	jobs := map[string]bool{}
	for _, m := range messages {
		if m.ToAgentId != lead {
			continue
		}
		switch b := m.Body.(type) {
		case *pb.Message_LeadBrief:
			briefs++
		case *pb.Message_Handoff:
			handoffs++
		case *pb.Message_JobFinished:
			if jobs[b.JobFinished.JobId] {
				t.Fatalf("job %s woke the lead twice", b.JobFinished.JobId)
			}
			jobs[b.JobFinished.JobId] = true
		default:
			t.Fatalf("unexpected lead wake: %T", m.Body)
		}
		if m.Delivery != pb.Delivery_DELIVERY_CONSUMED {
			t.Errorf("a message to the lead (%T) is %s after the run ended, want consumed", m.Body, m.Delivery)
		}
	}
	if briefs != 1 || handoffs != 3 || len(jobs) < minJobNotices || len(jobs) > maxJobNotices {
		t.Errorf("lead wakes: %d briefs, %d handoffs, %d job notices; want 1, 3 and %d to %d", briefs, handoffs, len(jobs), minJobNotices, maxJobNotices)
	}
	return len(jobs)
}
