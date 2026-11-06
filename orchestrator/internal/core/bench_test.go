package core_test

import (
	"fmt"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// BenchmarkDecide prices one fact on a run with a long history of commands behind it, which is the
// hot path of a daemon: most inputs are items from running agents. The cost must not follow the
// history.
func BenchmarkDecide(b *testing.B) {
	for _, commands := range []int{100, 10_000, 100_000} {
		b.Run(fmt.Sprintf("commands=%d", commands), func(b *testing.B) {
			t := &testing.T{}
			w, _, leadID := started(t, 1, steers)
			history := make([]core.Event, commands)
			for i := range history {
				history[i].Body = core.Receipt{Command: core.CommandID(fmt.Sprintf("old-%d", i)), Digest: "d"}
			}
			w.s = core.Replay(w.s, history)
			w.command(operator, core.SendMessage{Agent: leadID, Text: "go"})
			turn, _ := w.s.ActiveTurn(leadID)
			agent, _ := w.s.Agent(leadID)
			session := core.Session{Agent: leadID, N: agent.Session}
			b.ResetTimer()
			for i := range b.N {
				core.Decide(w.s, core.Input{At: epoch, Nonce: "n", Body: core.SessionFact{
					Session: session, Offset: 1000, Body: core.ItemObserved{
						ID: core.ItemID(fmt.Sprintf("item-%d", i)), Turn: turn.ID, Status: core.ItemRunning, Body: core.ToolCall{Tool: "bash"},
					},
				}})
			}
		})
	}
}
