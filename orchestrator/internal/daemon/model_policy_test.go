package daemon_test

import (
	"slices"
	"testing"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
)

func startOn(id string, harness pb.Harness, model string) *pb.Command {
	cmd := startRun(id)
	cmd.GetStartRun().Lead = &pb.AgentSpec{Harness: harness, Model: model}
	return cmd
}

var catalog = map[core.Harness][]core.ModelName{core.Codex: {"model-a", "model-b"}, core.ClaudeCode: {"model-c"}}

// A start on a model the host's policy forbids is refused with a typed receipt that names the model and
// lists what the host allows, and nothing opens: no run, and no session.
func TestAStartOnAForbiddenModelIsRefusedWithTheListAndOpensNoSession(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Models.Set(core.ModelPolicy{Allowed: []core.ModelName{"model-a"}, Known: catalog})
	receipt := dispatch(t, r, startOn("c1", pb.Harness_HARNESS_CODEX, "model-b"))
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_MODEL_NOT_ALLOWED || receipt.GetModel() != "model-b" || !slices.Equal(receipt.GetAllowedModels(), []string{"model-a"}) {
		t.Fatalf("receipt %v, want a model refusal naming model-b and listing model-a", receipt)
	}
	r.Settle(t)
	if n := len(r.Sidecars.Sessions()); n != 0 {
		t.Fatalf("%d sessions opened for a refused start", n)
	}
	if runs := r.Daemon.State().Runs(); len(runs) != 0 {
		t.Fatalf("a refused start made %d runs", len(runs))
	}
}

// A start that names no model, on a host that restricts them, runs on the first allowed model of the
// harness, a concrete one, and the session opens on it.
func TestAnUnpinnedStartRunsOnTheFirstAllowedModelOfItsHarness(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Models.Set(core.ModelPolicy{Allowed: []core.ModelName{"model-c", "model-b", "model-a"}, Known: catalog})
	receipt := dispatch(t, r, startOn("c1", pb.Harness_HARNESS_CODEX, ""))
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("receipt %v, want the start accepted", receipt)
	}
	r.Settle(t)
	sessions := r.Sidecars.Sessions()
	if len(sessions) != 1 {
		t.Fatalf("%d sessions, want the lead's one", len(sessions))
	}
	if got := sessions[0].Start.Launch.Spec; got.Harness != core.Codex || got.Model != "model-b" {
		t.Fatalf("the lead runs on %+v, want codex on model-b", got)
	}
}

// With the catalog unavailable an unpinned start cannot be resolved, so it is refused: not started on
// the harness's own default.
func TestAnUnpinnedStartThatCannotBeResolvedIsRefused(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Models.Set(core.ModelPolicy{Allowed: []core.ModelName{"model-a"}})
	receipt := dispatch(t, r, startOn("c1", pb.Harness_HARNESS_CODEX, ""))
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_MODEL_NOT_ALLOWED || receipt.GetModel() != "" {
		t.Fatalf("receipt %v, want a model refusal for an unnamed model", receipt)
	}
	r.Settle(t)
	if n := len(r.Sidecars.Sessions()); n != 0 {
		t.Fatalf("%d sessions opened", n)
	}
}

// The last look before a session opens holds whatever decided it: a launch already committed, under an
// older policy or none, never opens on a model the policy now forbids. The agent is lost with the typed
// reason and the sidecar is never asked.
func TestALaunchCommittedBeforeAPolicyNeverOpensOnAForbiddenModel(t *testing.T) {
	r := daemontest.NewRig(t)
	receipt := dispatch(t, r, startOn("c1", pb.Harness_HARNESS_CODEX, "model-b"))
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("receipt %v, want the start accepted under no policy", receipt)
	}
	r.Models.Set(core.ModelPolicy{Allowed: []core.ModelName{"model-a"}, Known: catalog})
	r.Settle(t)
	if n := len(r.Sidecars.Sessions()); n != 0 {
		t.Fatalf("%d sessions opened on a forbidden model", n)
	}
	run, _ := r.Daemon.State().Run(core.RunID(receipt.GetRunId()))
	lead, _ := r.Daemon.State().Agent(run.Lead)
	if lead.StartFailure != core.StartModelNotAllowed {
		t.Fatalf("the lead failed to start with %q, want %q", lead.StartFailure, core.StartModelNotAllowed)
	}
}

// A policy file that cannot be read allows nothing: the start is refused and lists no model.
func TestAnUnreadablePolicyStartsNothing(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Models.Set(core.ModelPolicy{Closed: true})
	receipt := dispatch(t, r, startOn("c1", pb.Harness_HARNESS_CODEX, "model-a"))
	if receipt.GetRefusal() != pb.Refusal_REFUSAL_MODEL_NOT_ALLOWED || len(receipt.GetAllowedModels()) != 0 {
		t.Fatalf("receipt %v, want a model refusal that lists none", receipt)
	}
}

// What a client is offered comes from the host: the snapshot carries the allowed models, whether the
// policy could be read, and what each harness runs.
func TestTheRunsSnapshotCarriesTheModelCatalog(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Models.Set(core.ModelPolicy{Allowed: []core.ModelName{"model-b"}, Known: catalog})
	w, first, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	got := client.WatchRunsSnapshot(first[0]).GetModels()
	if !slices.Equal(got.GetAllowedModels(), []string{"model-b"}) || got.GetUnreadablePolicy() {
		t.Fatalf("catalog %v, want model-b allowed and the policy readable", got)
	}
	byHarness := map[pb.Harness][]string{}
	for _, h := range got.GetHarnesses() {
		byHarness[h.GetHarness()] = h.GetModels()
	}
	if !slices.Equal(byHarness[pb.Harness_HARNESS_CODEX], []string{"model-a", "model-b"}) || !slices.Equal(byHarness[pb.Harness_HARNESS_CLAUDE_CODE], []string{"model-c"}) {
		t.Fatalf("harnesses %v, want what the catalog says", byHarness)
	}
}

// A policy that could not be read is said so in the catalog, with nothing allowed that a client could offer.
func TestTheCatalogSaysWhenThePolicyCouldNotBeRead(t *testing.T) {
	r := daemontest.NewRig(t)
	r.Models.Set(core.ModelPolicy{Closed: true, Known: catalog})
	w, first, err := r.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	if got := client.WatchRunsSnapshot(first[0]).GetModels(); !got.GetUnreadablePolicy() || len(got.GetAllowedModels()) != 0 {
		t.Fatalf("catalog %v, want an unreadable policy with nothing allowed", got)
	}
}
