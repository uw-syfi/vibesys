package core_test

import (
	"fmt"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// policies is every kind of host the rule distinguishes: no policy, one that lists models some harness
// runs, one that lists a model no harness is known to run, one that could not be read, each with the
// catalog there and without it.
func policies() map[string]core.ModelPolicy {
	known := map[core.Harness][]core.ModelName{core.Codex: {"m1", "m2"}, core.ClaudeCode: {"m3"}}
	out := map[string]core.ModelPolicy{}
	for catalog, k := range map[string]map[core.Harness][]core.ModelName{"catalog": known, "no catalog": nil} {
		for name, p := range map[string]core.ModelPolicy{
			"empty":         {},
			"one":           {Allowed: []core.ModelName{"m1"}},
			"two in order":  {Allowed: []core.ModelName{"m2", "m1"}},
			"other harness": {Allowed: []core.ModelName{"m3"}},
			"unknown":       {Allowed: []core.ModelName{"mx"}},
			"closed":        {Closed: true},
		} {
			p.Known = k
			out[fmt.Sprintf("%s/%s", name, catalog)] = p
		}
	}
	return out
}

// outcome is an independent statement of the rule: what a harness and model come to under a policy, and
// whether they are allowed at all.
func outcome(p core.ModelPolicy, harness core.Harness, model core.ModelName) (core.ModelName, bool) {
	if len(p.Allowed) == 0 && !p.Closed {
		return model, true
	}
	if p.Closed {
		return "", false
	}
	if model != "" {
		return model, slices.Contains(p.Allowed, model)
	}
	for _, a := range p.Allowed {
		if slices.Contains(p.Known[harness], a) {
			return a, true
		}
	}
	return "", false
}

var askedModels = []core.ModelName{"", "m1", "m2", "m3", "mx", "m5"}

// For every policy, harness and model asked for (named or not, listed or not), a start either begins a
// run whose lead is launched on a model the policy allows, or is refused with a receipt that names the
// model and lists what is allowed, and then no run, agent or session exists.
func TestAStartLaunchesOnAnAllowedModelOrIsRefusedAndStartsNothing(t *testing.T) {
	for name, policy := range policies() {
		for _, harness := range []core.Harness{core.Codex, core.ClaudeCode} {
			for _, asked := range askedModels {
				t.Run(fmt.Sprintf("%s/%s/%q", name, harness, asked), func(t *testing.T) {
					w := newScript(t)
					w.models = policy
					r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 2, Lead: core.AgentSpec{Harness: harness, Model: asked}, Brief: "go"})
					want, ok := outcome(policy, harness, asked)
					if !ok {
						if r.Refusal != core.RefuseModelNotAllowed || r.Model != asked {
							t.Fatalf("receipt %+v, want a model refusal naming %q", r, asked)
						}
						if wantList := allowedOf(policy); !slices.Equal(r.AllowedModels, wantList) {
							t.Fatalf("the refusal lists %v, want %v", r.AllowedModels, wantList)
						}
						if len(w.s.Runs()) != 0 || len(w.effects) != 0 {
							t.Fatalf("a refused start left %d runs and effects %v", len(w.s.Runs()), w.kinds())
						}
						return
					}
					if r.Refusal != "" {
						t.Fatalf("an allowed start was refused: %+v", r)
					}
					launched := launches(w.effects)
					if len(launched) != 1 || launched[0].Spec.Model != want || launched[0].Spec.Harness != harness {
						t.Fatalf("launched %+v, want one session on %s %q", launched, harness, want)
					}
					if policy.Restricts() && !slices.Contains(policy.Allowed, launched[0].Spec.Model) {
						t.Fatalf("a session was launched on %q, which the policy does not allow", launched[0].Spec.Model)
					}
				})
			}
		}
	}
}

// The agents a start adds to the run's allowance are held to the policy as the lead is: one that the
// policy forbids refuses the whole start, and an allowed one is kept as a concrete model, so the run's
// allowance never holds a model the policy does not allow.
func TestAnAddedAgentIsHeldToThePolicyAndTheAllowanceHoldsNothingForbidden(t *testing.T) {
	for name, policy := range policies() {
		for _, harness := range []core.Harness{core.Codex, core.ClaudeCode} {
			for _, asked := range askedModels {
				t.Run(fmt.Sprintf("%s/%s/%q", name, harness, asked), func(t *testing.T) {
					w := newScript(t)
					w.models = policy
					leadSpec := core.AgentSpec{Harness: core.Codex, Model: "m1"}
					r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", Lead: leadSpec, Allow: []core.AgentChoice{{Harness: harness, Model: asked}}, Brief: "go"})
					_, leadOK := outcome(policy, leadSpec.Harness, leadSpec.Model)
					want, addedOK := outcome(policy, harness, asked)
					if !leadOK || !addedOK {
						if r.Refusal != core.RefuseModelNotAllowed || len(w.s.Runs()) != 0 {
							t.Fatalf("receipt %+v with %d runs, want a model refusal and no run", r, len(w.s.Runs()))
						}
						return
					}
					run, _ := w.s.Run(r.Run)
					for _, c := range run.Allowed {
						if policy.Restricts() && !slices.Contains(policy.Allowed, c.Model) {
							t.Fatalf("the allowance holds %+v, which the policy forbids", c)
						}
					}
					if !slices.Contains(run.Allowed, core.AgentChoice{Harness: harness, Model: want}) {
						t.Fatalf("the allowance %v lacks %s %q", run.Allowed, harness, want)
					}
				})
			}
		}
	}
}

// A run started with no policy (or under an older one) never launches a subagent on a model the policy of
// the moment forbids: every spawn, whatever the run was allowed to use, is refused or lands on an allowed model.
func TestASpawnNeverLaunchesOnAModelTheCurrentPolicyForbids(t *testing.T) {
	allowance := []core.AgentChoice{{Harness: core.Codex}, {Harness: core.Codex, Model: "m1"}, {Harness: core.Codex, Model: "m5"}, {Harness: core.ClaudeCode, Model: "m3"}}
	for name, policy := range policies() {
		for _, harness := range []core.Harness{core.Codex, core.ClaudeCode} {
			for _, asked := range askedModels {
				t.Run(fmt.Sprintf("%s/%s/%q", name, harness, asked), func(t *testing.T) {
					w := newScript(t)
					r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", FanoutLimit: 3, Lead: core.AgentSpec{Harness: core.Codex}, Allow: allowance, Brief: "go"})
					w.open(r.Agent, steers)
					w.endTurn(r.Agent, core.TurnCompleted)
					w.models = policy
					before := len(w.s.Agents(r.Run))
					got := w.command(core.AgentCaller{Agent: r.Agent}, core.SpawnAgent{Run: r.Run, Role: core.RoleWorker, Spec: core.AgentSpec{Harness: harness, Model: asked}, Brief: "work"})
					for _, l := range launches(w.effects) {
						if _, ok := outcome(policy, l.Spec.Harness, l.Spec.Model); !ok || policy.Restricts() && l.Spec.Model == "" {
							t.Fatalf("launched %+v under %+v", l, policy)
						}
					}
					if got.Refusal == core.RefuseModelNotAllowed {
						if len(w.s.Agents(r.Run)) != before || len(launches(w.effects)) != 0 {
							t.Fatalf("a refused spawn left agents %d (was %d) and effects %v", len(w.s.Agents(r.Run)), before, w.kinds())
						}
						if got.Run != r.Run {
							t.Fatalf("the refusal names run %q, want %q", got.Run, r.Run)
						}
					}
				})
			}
		}
	}
}

// A policy that could not be read allows nothing, and the receipt says there is no list to choose
// from, which is not the same as a list that is empty because the host has no policy.
func TestAnUnreadablePolicyRefusesWithNoListOfModels(t *testing.T) {
	w := newScript(t)
	w.models = core.ModelPolicy{Closed: true, Allowed: []core.ModelName{"m1"}}
	r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", Lead: core.AgentSpec{Harness: core.Codex, Model: "m1"}, Brief: "go"})
	if r.Refusal != core.RefuseModelNotAllowed || len(r.AllowedModels) != 0 {
		t.Fatalf("receipt %+v, want a model refusal that lists nothing", r)
	}
}

func allowedOf(p core.ModelPolicy) []core.ModelName {
	if p.Closed {
		return nil
	}
	return p.Allowed
}

func launches(effects []core.Effect) []core.LaunchSession {
	var out []core.LaunchSession
	for _, e := range effects {
		if l, ok := e.(core.LaunchSession); ok {
			out = append(out, l)
		}
	}
	return out
}
