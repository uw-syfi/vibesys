package core_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// done is a handoff of a worker that has nothing to offer, for the tests that only need one.
func done() core.Handoff {
	return core.Handoff{Claim: "done", Body: core.WorkerNoCandidate{Blocker: "none"}}
}

// bodies draws one body of each kind, and no body, with fields of any content.
func bodies(r *rand.Rand) []core.HandoffBody {
	s := func() string { return fmt.Sprintf("v%d", r.Uint32()) }
	return []core.HandoffBody{
		core.WorkerCandidate{Commit: core.Commit(s()), ParentCommit: core.Commit(s()), Branch: s(), Checks: s(), Measurement: s()},
		core.WorkerNoCandidate{Blocker: s()},
		core.PlannerPlan{Plan: s(), Confidence: s()},
		core.JudgeFinding{Commit: core.Commit(s()), Verdict: core.VerdictPass, Reasons: s()},
		core.CombinerResult{Commit: core.Commit(s()), Branch: s(), Applied: s()},
		core.ProfilerFindings{Question: s(), Revision: core.Commit(s()), Answer: s()},
		nil,
	}
}

// The core holds a handoff to its poster's role, whatever reaches it: of every role and every body,
// exactly the matching pairs are posted, the others are refused with a typed refusal that changes
// nothing, and deciding the same input again gives the same answer.
func TestACoreAcceptsAHandoffOnlyFromTheRoleItsBodyBelongsTo(t *testing.T) {
	r := rand.New(rand.NewPCG(41, 42))
	subagents := []core.AgentRole{core.RolePlanner, core.RoleWorker, core.RoleJudge, core.RoleCombiner, core.RoleProfiler}
	for range 20 {
		for _, role := range subagents {
			for _, body := range bodies(r) {
				name := fmt.Sprintf("%s posting %T", role, body)
				w, run, leadID := started(t, 2, steers)
				spawned := w.command(core.AgentCaller{Agent: leadID}, core.SpawnAgent{Run: run, Role: role, Brief: "work"})
				w.open(spawned.Agent, steers)
				cmd := core.Command{ID: "post", Digest: "d", Caller: core.AgentCaller{Agent: spawned.Agent}, Body: core.PostHandoff{
					Handoff: core.Handoff{PlanNumber: r.Uint32(), Claim: "c", Body: body},
				}}

				in := core.Input{At: w.now.Add(time.Second), Nonce: "n-post", Body: cmd}
				s1, e1, f1 := core.Decide(w.s, in)
				s2, e2, f2 := core.Decide(w.s, in)
				if !reflect.DeepEqual(s1, s2) || !reflect.DeepEqual(e1, e2) || !reflect.DeepEqual(f1, f2) {
					t.Fatalf("%s: deciding one input twice gave two answers", name)
				}

				before := len(w.log)
				w.step(cmd)
				receipt, _ := w.s.Recall("post", "d")
				posted := map[core.MessageID]bool{}
				for _, e := range w.log[before:] {
					if m, ok := e.Body.(core.Message); ok {
						if _, isHandoff := m.Body.(core.Handoff); isHandoff {
							posted[m.ID] = true
						}
					}
				}
				handoffs := len(posted)
				agent, _ := w.s.Agent(spawned.Agent)
				if (core.Handoff{Body: body}).Role() == role {
					if receipt.Refusal != "" || handoffs != 1 || !agent.HandedOff {
						t.Fatalf("%s: refusal %q, %d handoffs posted, handed off %v", name, receipt.Refusal, handoffs, agent.HandedOff)
					}
				} else if receipt.Refusal != core.RefuseHandoffNotYours || handoffs != 0 || agent.HandedOff {
					t.Fatalf("%s: refusal %q, %d handoffs posted, handed off %v; want a refusal that posts nothing", name, receipt.Refusal, handoffs, agent.HandedOff)
				}

				// A repeat of the command answers as before and posts nothing more.
				again := len(w.log)
				w.step(cmd)
				if repeated, _ := w.s.Recall("post", "d"); !reflect.DeepEqual(repeated, receipt) {
					t.Fatalf("%s: the repeat answered %+v, the first %+v", name, repeated, receipt)
				}
				for _, e := range w.log[again:] {
					if m, ok := e.Body.(core.Message); ok {
						t.Fatalf("%s: the repeat posted %+v", name, m)
					}
				}
			}
		}
	}
}

// Every body names the role it belongs to, and each subagent role has a body.
func TestEverySubagentRoleHasABodyThatNamesIt(t *testing.T) {
	var named []core.AgentRole
	for _, b := range bodies(rand.New(rand.NewPCG(1, 2))) {
		if b != nil {
			named = append(named, core.Handoff{Body: b}.Role())
		}
	}
	for _, role := range []core.AgentRole{core.RolePlanner, core.RoleWorker, core.RoleJudge, core.RoleCombiner, core.RoleProfiler} {
		if !slices.Contains(named, role) {
			t.Errorf("no handoff body belongs to %s", role)
		}
	}
	if (core.Handoff{}).Role() != "" {
		t.Error("a handoff with no body names a role")
	}
}
