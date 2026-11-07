//go:build e2e

package e2e

import (
	"testing"
	"time"

	"google.golang.org/protobuf/encoding/protojson"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// awaitLeadDeliveries reads the typed mailbox of the run's snapshot, rather than waiting for a
// period of silence or inferring a wake from prompt text, until every message to the lead is
// consumed. The run ends by itself once the lead has reported, so the snapshot is read as `run get`
// serves it, which holds the messages of a run that is over. This fixture submits two baseline jobs,
// four worker jobs, and two post-promotion jobs. The bound only guards against a delivery that never
// comes.
func (r *rig) awaitLeadDeliveries(lead string) {
	r.t.Helper()
	deadline := time.Now().Add(bound)
	for {
		var snapshot pb.RunsSnapshot
		if err := protojson.Unmarshal([]byte(r.client("run", "get", "--json")), &snapshot); err != nil {
			r.t.Fatal(err)
		}
		messages := map[string]*pb.Message{}
		for _, m := range snapshot.GetMessages() {
			messages[m.Id] = m
		}
		if leadDeliveriesComplete(r.t, messages, lead) {
			return
		}
		if time.Now().After(deadline) {
			r.t.Fatalf("waiting for all lead deliveries: %d messages after %s", len(messages), bound)
		}
		time.Sleep(100 * time.Millisecond)
	}
}

func leadDeliveriesComplete(t *testing.T, messages map[string]*pb.Message, lead string) bool {
	t.Helper()
	briefs, handoffs := 0, 0
	jobs := map[string]bool{}
	consumed := true
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
		consumed = consumed && m.Delivery == pb.Delivery_DELIVERY_CONSUMED
	}
	if briefs > 1 || handoffs > 3 || len(jobs) > 8 {
		t.Fatalf("unexpected lead wakes: %d briefs, %d handoffs, %d jobs", briefs, handoffs, len(jobs))
	}
	return briefs == 1 && handoffs == 3 && len(jobs) == 8 && consumed
}
