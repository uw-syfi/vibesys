package wire_test

import (
	"testing"
	"time"

	"google.golang.org/protobuf/proto"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/cost"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

func dollarsOf(c float64) *float64 { return &c }

func tokensOf(n uint64) *uint64 { return &n }

// A cost view reaches a client as the contract states it, whatever it holds: no priced turn at all,
// priced and unpriced turns, agents that ran no turn, and runs with and without a limit. Every
// message meets the contract's rules, survives the wire, and says in the order it is given what the
// view said.
func TestCostMessageMeetsTheContract(t *testing.T) {
	begin := time.Date(2026, time.November, 1, 9, 0, 0, 0, time.UTC)
	agents := []core.Agent{
		{ID: "lead", Role: core.RoleLead, Spec: core.AgentSpec{Harness: core.ClaudeCode}},
		{ID: "w1", Role: core.RoleWorker, Spec: core.AgentSpec{Harness: core.Codex, Model: "model-x"}},
		{ID: "idle", Role: core.RoleJudge, Spec: core.AgentSpec{Harness: core.ClaudeCode, Model: "model-small"}},
	}
	turn := func(agent core.AgentID, ordinal uint32, at time.Duration, u core.Usage) core.Turn {
		return core.Turn{ID: core.TurnID(string(agent) + string(rune('0'+ordinal))), Agent: agent, Ordinal: ordinal, Status: core.TurnCompleted, Usage: u, EndedAt: begin.Add(at)}
	}
	cases := map[string]func(l *cost.Ledger){
		"nothing": func(*cost.Ledger) {},
		"unpriced only": func(l *cost.Ledger) {
			l.Add(turn("w1", 1, time.Hour, core.Usage{InputTokens: tokensOf(10)}), agents[1])
		},
		"priced and unpriced": func(l *cost.Ledger) {
			l.Add(turn("lead", 1, time.Minute, core.Usage{InputTokens: tokensOf(5), CostUSD: dollarsOf(0.5)}), agents[0])
			l.Add(turn("w1", 1, 90*time.Minute, core.Usage{OutputTokens: tokensOf(7)}), agents[1])
			l.Add(turn("lead", 2, 3*time.Hour, core.Usage{CostUSD: dollarsOf(1.25), ReasoningTokens: tokensOf(2)}), agents[0])
		},
		"only free turns": func(l *cost.Ledger) {
			l.Add(turn("lead", 1, time.Minute, core.Usage{CostUSD: dollarsOf(0)}), agents[0])
		},
	}
	for name, fill := range cases {
		for _, limit := range []float64{0, 5} {
			t.Run(name, func(t *testing.T) {
				var l cost.Ledger
				for _, a := range agents {
					l.Agent(a)
				}
				fill(&l)
				v := l.View(core.Run{ID: "run-1", CreatedAt: begin, MaxCostUSD: limit}, begin.Add(4*time.Hour))
				msg := wire.CostMessage(v, 42)
				if err := wire.Validate(msg); err != nil {
					t.Fatalf("the contract refuses it: %v\n%v", err, msg)
				}
				data, err := proto.Marshal(msg)
				if err != nil {
					t.Fatal(err)
				}
				var back pb.RunCost
				if err := proto.Unmarshal(data, &back); err != nil || !proto.Equal(msg, &back) {
					t.Fatalf("lost on the wire (%v):\n got %v\nwant %v", err, &back, msg)
				}
				if msg.Sequence != 42 || msg.RunId != "run-1" || msg.Turns != v.Turns || msg.UnpricedTurns != v.Unpriced {
					t.Fatalf("header %v does not say what the view does: %+v", msg, v)
				}
				if (msg.MaxCostUsd != nil) != (limit > 0) {
					t.Fatalf("limit %v became %v", limit, msg.MaxCostUsd)
				}
				if (msg.Total.CostUsd != nil) != (v.Total.CostUSD != nil) || (msg.LastHourUsd != nil) != (v.LastHour != nil) {
					t.Fatalf("a missing cost became a number or the other way round: %v", msg)
				}
				if len(msg.Roles) != len(v.Roles) || len(msg.Models) != len(v.Models) || len(msg.Series.Buckets) != len(v.Series.Buckets) {
					t.Fatalf("lists differ in length: %v", msg)
				}
				for i, r := range v.Roles {
					if got, _ := wire.RoleIn(msg.Roles[i].Role); got != r.Role || msg.Roles[i].Agents != r.Agents {
						t.Errorf("type %d is %v, want %v", i, msg.Roles[i], r)
					}
				}
			})
		}
	}
}
