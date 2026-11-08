//go:build e2e

package e2e

import (
	"slices"
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// fixtureJobs is how many jobs the fixture submits: the two baseline jobs, the four workers' jobs and the
// two the lead awaits after the promotion. The lead is told of a job only when the daemon reads the job
// feed (on a timer) and finds the job settled while the run is live, and the run ends the moment the lead
// has reported, so a fast run can end between two reads and be told of none. How many notices come is
// timing. What holds is that none repeats, there are no more than the jobs submitted, and each is
// consumed. The rule that decides which jobs are told is held by the core tests in
// orchestrator/internal/core/job_notice_test.go.
const fixtureJobs = 8

// awaitLeadDeliveries waits until the run is over, then reads the typed mailbox of its snapshot as
// `run get` serves it (it holds the messages of a run that is over) and returns the ids of the jobs
// the lead was told of. Every message to the lead must have been consumed: the run ends only when
// nothing is in transit, so a message that is not consumed was lost. The bound only guards against a
// run that never ends.
func (r *rig) awaitLeadDeliveries(run, lead string) (told []string) {
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

// leadDeliveries checks what woke the lead: one brief, three handoffs, and job notices of at most
// fixtureJobs jobs, each job at most once, all consumed. It returns the ids of the jobs told, in order.
func leadDeliveries(t *testing.T, messages []*pb.Message, lead string) []string {
	t.Helper()
	briefs, handoffs := 0, 0
	var jobs []string
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
			if slices.Contains(jobs, b.JobFinished.JobId) {
				t.Fatalf("job %s woke the lead twice", b.JobFinished.JobId)
			}
			jobs = append(jobs, b.JobFinished.JobId)
		default:
			t.Fatalf("unexpected lead wake: %T", m.Body)
		}
		if m.Delivery != pb.Delivery_DELIVERY_CONSUMED {
			t.Errorf("a message to the lead (%T) is %s after the run ended, want consumed", m.Body, m.Delivery)
		}
	}
	if briefs != 1 || handoffs != 3 || len(jobs) > fixtureJobs {
		t.Errorf("lead wakes: %d briefs, %d handoffs, %d job notices; want 1, 3 and at most %d", briefs, handoffs, len(jobs), fixtureJobs)
	}
	return jobs
}
