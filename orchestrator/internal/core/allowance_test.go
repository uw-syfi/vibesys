package core_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// A run started on one harness and model, with nothing added, may spawn only there. This is the
// live finding: `--harness codex --model M` ran a lead whose judges came up on claude_code's default.
func TestASpawnOnAHarnessTheOperatorDidNotChooseIsRefusedAndNothingStarts(t *testing.T) {
	codex := core.AgentSpec{Harness: core.Codex, Model: "gpt-m"}
	w := newScript(t)
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 2, Lead: codex, Brief: "go"})
	w.open(r.Agent, steers)
	w.endTurn(r.Agent, core.TurnCompleted)
	before := len(w.s.Agents(r.Run))

	refused := w.command(core.AgentCaller{Agent: r.Agent}, core.SpawnAgent{
		Run: r.Run, Role: core.RoleJudge, Spec: core.AgentSpec{Harness: core.ClaudeCode}, Brief: "judge",
	})
	if refused.Refusal != core.RefuseAgentNotAllowed {
		t.Fatalf("a spawn on claude_code in a codex run: refusal = %q", refused.Refusal)
	}
	if want := []core.AgentChoice{{Harness: core.Codex, Model: "gpt-m"}}; !reflect.DeepEqual(refused.Allowed, want) {
		t.Fatalf("the refusal lists %v, want %v", refused.Allowed, want)
	}
	if got := len(w.s.Agents(r.Run)); got != before || len(w.effects) != 0 {
		t.Fatalf("a refused spawn left %d agents (was %d) and effects %v", got, before, w.kinds())
	}

	// The same run takes an absent harness and an absent model from the lead: gpt-m on codex.
	inherited := w.spawn(r.Run, r.Agent, steers)
	if got, _ := w.s.Agent(inherited); got.Spec.Harness != core.Codex || got.Spec.Model != "gpt-m" {
		t.Fatalf("a bare spawn runs on %+v, want the lead's harness and model", got.Spec)
	}
	// And a model the operator did not name on the same harness is refused too.
	other := w.command(core.AgentCaller{Agent: r.Agent}, core.SpawnAgent{
		Run: r.Run, Role: core.RoleJudge, Spec: core.AgentSpec{Model: "gpt-pricey"}, Brief: "judge",
	})
	if other.Refusal != core.RefuseAgentNotAllowed {
		t.Fatalf("another model on the lead's harness: refusal = %q", other.Refusal)
	}
}

func TestAnAllowedHarnessTakesItsFirstAllowedModelWhenTheSpawnNamesNone(t *testing.T) {
	w := newScript(t)
	r := w.command(operator, core.StartRun{
		Repository: "repo", Task: "task", FanoutLimit: 3, Lead: lead, Brief: "go",
		Allow: []core.AgentChoice{{Harness: core.Codex, Model: "gpt-a"}, {Harness: core.Codex, Model: "gpt-b"}, {Harness: core.Codex}},
	})
	w.open(r.Agent, steers)
	w.endTurn(r.Agent, core.TurnCompleted)
	ask := func(spec core.AgentSpec) core.Receipt {
		return w.command(core.AgentCaller{Agent: r.Agent}, core.SpawnAgent{Run: r.Run, Role: core.RoleWorker, Spec: spec, Brief: "work"})
	}
	first := ask(core.AgentSpec{Harness: core.Codex})
	if got, _ := w.s.Agent(first.Agent); got.Spec.Model != "gpt-a" {
		t.Fatalf("an unnamed model on codex is %q, want the first allowed, gpt-a", got.Spec.Model)
	}
	named := ask(core.AgentSpec{Harness: core.Codex, Model: "gpt-b"})
	if named.Refusal != "" {
		t.Fatalf("a named allowed model was refused: %q", named.Refusal)
	}
	if bad := ask(core.AgentSpec{Harness: core.Codex, Model: "gpt-c"}); bad.Refusal != core.RefuseAgentNotAllowed {
		t.Fatalf("a model nobody allowed: refusal = %q", bad.Refusal)
	}
}

// For any lead, any allowance and any spawn request, Decide agrees with an independent statement of
// the rule: a session is never started outside the allowance, the answer is a named outcome, a
// refusal changes nothing but its receipt, and a replay of the log answers the same.
func TestASpawnIsWithinTheAllowanceOrRefusedWithWhatIsAllowed(t *testing.T) {
	harnesses := []core.Harness{"", core.ClaudeCode, core.Codex}
	models := []core.ModelName{"", "m1", "m2", "m3"}
	pickOf := func(r *rand.Rand) core.AgentChoice {
		return core.AgentChoice{Harness: harnesses[1+r.IntN(2)], Model: models[r.IntN(len(models))]}
	}
	for seed := range uint64(400) {
		r := rand.New(rand.NewPCG(seed, 7))
		leadChoice := pickOf(r)
		var extra []core.AgentChoice
		for range r.IntN(4) {
			extra = append(extra, pickOf(r))
		}
		allowed := []core.AgentChoice{leadChoice}
		for _, c := range extra {
			if !slices.Contains(allowed, c) {
				allowed = append(allowed, c)
			}
		}

		w := newScript(t)
		start := w.command(operator, core.StartRun{
			Repository: "repo", Task: "task", FanoutLimit: 1000, Brief: "go", Allow: extra,
			Lead: core.AgentSpec{Harness: leadChoice.Harness, Model: leadChoice.Model},
		})
		w.open(start.Agent, steers)
		w.endTurn(start.Agent, core.TurnCompleted)
		if run, _ := w.s.Run(start.Run); !reflect.DeepEqual(run.Allowed, allowed) {
			t.Fatalf("seed %d: the run allows %v, want the lead then the additions: %v", seed, run.Allowed, allowed)
		}

		for range 30 {
			ask := core.AgentSpec{Harness: harnesses[r.IntN(len(harnesses))], Model: models[r.IntN(len(models))]}
			harness := ask.Harness
			if harness == "" {
				harness = leadChoice.Harness
			}
			want, ok := core.AgentChoice{Harness: harness, Model: ask.Model}, false
			if ask.Model != "" {
				ok = slices.Contains(allowed, want)
			} else if i := slices.IndexFunc(allowed, func(c core.AgentChoice) bool { return c.Harness == harness }); i >= 0 {
				want, ok = allowed[i], true
			}

			agents := len(w.s.Agents(start.Run))
			got := w.command(core.AgentCaller{Agent: start.Agent}, core.SpawnAgent{Run: start.Run, Role: core.RoleWorker, Spec: ask, Brief: "work"})
			switch {
			case ok && got.Refusal != "":
				t.Fatalf("seed %d: %+v inside %v was refused: %q", seed, ask, allowed, got.Refusal)
			case !ok && got.Refusal != core.RefuseAgentNotAllowed:
				t.Fatalf("seed %d: %+v outside %v: refusal = %q", seed, ask, allowed, got.Refusal)
			case !ok && !reflect.DeepEqual(got.Allowed, allowed):
				t.Fatalf("seed %d: the refusal lists %v, want %v", seed, got.Allowed, allowed)
			case !ok && (len(w.s.Agents(start.Run)) != agents || slices.Contains(w.kinds(), "LaunchSession")):
				t.Fatalf("seed %d: a refused spawn started something: %v", seed, w.kinds())
			case ok:
				spec, _ := w.s.Agent(got.Agent)
				if spec.Spec.Harness != want.Harness || spec.Spec.Model != want.Model {
					t.Fatalf("seed %d: asked %+v, the agent runs on %+v, want %+v", seed, ask, spec.Spec, want)
				}
				w.fact(got.Agent, core.SessionClosed{}) // keeps the live count small; irrelevant to the rule.
			}
			if again := core.Replay(core.State{}, w.log); !receiptsEqual(t, again, got) {
				t.Fatalf("seed %d: replay answers %s differently", seed, fmt.Sprint(got.Command))
			}
		}
	}
}

func receiptsEqual(t *testing.T, s core.State, want core.Receipt) bool {
	t.Helper()
	got, recall := s.Recall(want.Command, want.Digest)
	return recall == core.RecallRepeat && reflect.DeepEqual(got, want)
}
