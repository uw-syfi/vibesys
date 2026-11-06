package clientcli_test

import (
	"context"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// awaitConsumed waits on the stream, with no timer, until the operator's message with this text
// has been taken by its agent.
func (l *live) awaitConsumed(t testing.TB, text string) {
	t.Helper()
	ctx, cancel := context.WithTimeout(context.Background(), guard)
	defer cancel()
	stream, err := l.op.WatchRuns(ctx, 0)
	if err != nil {
		t.Fatal(err)
	}
	taken := func(m *pb.Message) bool {
		return m.GetText() == text && m.GetDelivery() == pb.Delivery_DELIVERY_CONSUMED
	}
	for {
		frame, err := stream.Recv()
		if err != nil {
			t.Fatalf("waiting for the message to be taken: %v", err)
		}
		snapshot, event, _ := client.DecodeWatchRuns(frame)
		for _, m := range snapshot.GetMessages() {
			if taken(m) {
				return
			}
		}
		if taken(event.GetMessage()) {
			return
		}
	}
}
