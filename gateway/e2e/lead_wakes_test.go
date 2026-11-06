//go:build e2e

package e2e

import (
	"context"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/hosted"
)

// awaitLeadDeliveries observes the typed mailbox, rather than waiting for a
// period of silence or inferring a wake from prompt text. This fixture submits
// two baseline jobs, four worker jobs, and two post-promotion jobs.
func (r *rig) awaitLeadDeliveries(lead string) {
	r.t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), bound)
	defer cancel()
	connection := r.dial(ctx)
	defer connection.Close()
	stream, err := pb.NewOrchestratorServiceClient(connection).WatchRuns(ctx, &pb.WatchRunsRequest{})
	if err != nil {
		r.t.Fatal(err)
	}
	messages := map[string]*pb.Message{}
	for {
		frame, err := stream.Recv()
		if err != nil {
			r.t.Fatalf("waiting for all lead deliveries: %v", err)
		}
		snap, event, _ := hosted.DecodeWatchRuns(frame)
		if snap != nil {
			clear(messages)
			for _, m := range snap.Messages {
				messages[m.Id] = m
			}
		}
		if m := event.GetMessageUpdated(); m != nil {
			messages[m.Id] = m
		}
		if leadDeliveriesComplete(r.t, messages, lead) {
			return
		}
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
