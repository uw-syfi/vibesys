package daemon_test

import (
	"context"
	"strings"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

// The process holding an agent is sent words. It is never sent the message they came from, so it
// never learns a skill's name or what a run's environment holds.
func TestASessionIsSentTheWordsOfAMessageAndNotTheMessage(t *testing.T) {
	r := daemontest.NewRig(t)
	_, lead := beginRun(t, r)
	session := r.Sidecars.Session(core.Session{Agent: lead.ID, N: 1})

	first := session.Received[0].GetStartTurn()
	if first == nil {
		t.Fatalf("the lead's session was first sent %v, want a turn", session.Received[0])
	}
	for _, want := range []string{"`vibesys:optimize`", "`task`", "--fanout 1", "go"} {
		if !strings.Contains(first.GetPrompt(), want) {
			t.Errorf("the lead's first prompt is %q, want it to hold %q", first.GetPrompt(), want)
		}
	}

	// A turn ends, and the operator's note to the lead starts the next one, worded as it was sent.
	r.Settle(t)
	if _, err := r.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{
		Id: "tell", Body: &pb.Command_SendMessage{SendMessage: &pb.SendMessage{AgentId: string(lead.ID), Text: "  look at the cache\n"}},
	}}); err != nil {
		t.Fatal(err)
	}
	r.Settle(t)
	var prompts []string
	for _, cmd := range session.Received {
		switch b := cmd.GetBody().(type) {
		case *pb.SessionCommand_StartTurn:
			prompts = append(prompts, b.StartTurn.GetPrompt())
		case *pb.SessionCommand_Steer:
			prompts = append(prompts, b.Steer.GetPrompt())
			if b.Steer.GetMessageId() == "" {
				t.Errorf("a steer names no message: %v", cmd)
			}
		}
	}
	if len(prompts) != 2 || prompts[1] != "look at the cache" {
		t.Fatalf("the lead was sent %q, want its brief and then the operator's words", prompts)
	}
}
