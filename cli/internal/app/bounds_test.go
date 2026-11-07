package app_test

import (
	"math/rand/v2"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// servingTask is a task that scores throughput and reports two latencies beside it, as a serving
// benchmark does.
func servingTask() task.Tree {
	files := tasktest.Files("task")
	dir := task.TasksDir + "/task/"
	files[dir+task.ManifestName] = task.File{Data: []byte(`
version = "0.1"

[agent]
domain = "kernels"

[check]
command = ["./check"]

[measure]
command = ["./bench"]
result_protocol = 2
`)}
	files[dir+task.ObjectiveName] = task.File{Data: []byte(`
[[objective]]
name = "tok_s"
direction = "max"

[[objective]]
name = "ttft_ms"
direction = "min"

[[objective]]
name = "tpot_ms"
direction = "min"

[pareto]
relative_noise = 0.02
`)}
	return files
}

func statement(objective string, value float64) core.BoundStatement {
	return core.BoundStatement{Objective: objective, Value: value, Unit: "unit", Note: "derived from the hardware"}
}

// A declared bound is read against the task as its commit has it, takes the objective's direction
// from the task, and says who declared it and when from the call and the daemon's clock. A
// revision is a new entry: nothing is replaced.
func TestADeclaredBoundIsStampedFromTheCallAndNeverReplaced(t *testing.T) {
	w := newWorld(t)
	w.tasks.Put("/repo", commit, servingTask())
	a := w.newApp()
	ctx := ctxFor(t)
	other := core.CallContext{Scope: scope, Run: "run-2", Harness: "h", Agent: "agent-2"}

	first, err := a.DeclareBound(ctx, call, commit, statement("tok_s", 15600))
	if err != nil {
		t.Fatal(err)
	}
	if first.Seq != 1 || first.Direction != core.Maximize || first.Objective != "tok_s" || first.Value != 15600 ||
		first.Origin.Run != "run-1" || first.Origin.Agent != "agent-1" || first.Origin.At.IsZero() {
		t.Fatalf("first bound = %+v, want place 1, the task's direction and the call's origin at the clock's time", first)
	}
	latency, err := a.DeclareBound(ctx, call, commit, statement("ttft_ms", 74))
	if err != nil || latency.Seq != 2 || latency.Direction != core.Minimize {
		t.Fatalf("latency bound = %+v, %v; want place 2 and minimize", latency, err)
	}
	revised, err := a.DeclareBound(ctx, other, commit, statement("tok_s", 12000))
	if err != nil || revised.Seq != 3 || revised.Origin.Agent != "agent-2" || revised.Origin.Run != "run-2" {
		t.Fatalf("revision = %+v, %v; want place 3 declared by the other call", revised, err)
	}

	if !first.Origin.At.Before(latency.Origin.At) || !latency.Origin.At.Before(revised.Origin.At) {
		t.Errorf("the times %v, %v, %v do not follow the daemon's clock", first.Origin.At, latency.Origin.At, revised.Origin.At)
	}
	got, err := a.Bounds(ctx, scope)
	if err != nil || len(got) != 3 || got[0] != first || got[1] != latency || got[2] != revised {
		t.Fatalf("Bounds = %+v, %v; want all three versions, oldest first, the first unchanged", got, err)
	}
	if standing := core.StandingBounds(got); len(standing) != 2 || standing[0] != latency || standing[1] != revised {
		t.Fatalf("standing = %+v, want the latency bound and the revision", standing)
	}
}

// An objective the task does not declare is refused with an error that names it, and nothing is
// recorded: the refused call makes no store either.
func TestABoundForAnUnknownObjectiveIsRefusedAndRecordsNothing(t *testing.T) {
	w := newWorld(t)
	w.tasks.Put("/repo", commit, servingTask())
	a := w.newApp()
	ctx := ctxFor(t)

	_, err := a.DeclareBound(ctx, call, commit, statement("goodput", 10))
	wantInvalid(t, "an unknown objective", err)
	if err == nil || !strings.Contains(err.Error(), `"goodput"`) || !strings.Contains(err.Error(), `"tok_s"`) {
		t.Fatalf("the refusal %v does not name the objective and the known ones", err)
	}
	if known, err := w.stores.Known(ctx); err != nil || len(known) != 0 {
		t.Fatalf("known stores = %v, %v; a refused declaration must not make one", known, err)
	}
	if got, err := a.Bounds(ctx, scope); err != nil || len(got) != 0 {
		t.Fatalf("Bounds = %+v, %v; want none", got, err)
	}
}

func TestABoundNeedsAFullCommitOfTheTask(t *testing.T) {
	w := newWorld(t)
	w.tasks.Put("/repo", commit, servingTask())
	a := w.newApp()
	ctx := ctxFor(t)
	_, err := a.DeclareBound(ctx, call, "HEAD", statement("tok_s", 10))
	wantInvalid(t, "a revision that is not a commit", err)
	if err == nil || !strings.Contains(err.Error(), "commit") {
		t.Errorf("the refusal %v does not name the commit", err)
	}
	_, err = a.DeclareBound(ctx, call, core.Commit(strings.Repeat("a", 40)), statement("tok_s", 10))
	wantNotFound(t, "a commit the repository does not have", err)
	if got, err := a.Bounds(ctx, scope); err != nil || len(got) != 0 {
		t.Fatalf("Bounds = %+v, %v; want none", got, err)
	}
}

// Whatever mix of objectives, values and calls, the log holds exactly the declarations that were
// accepted, in the order they were made and numbered from 1, and the standing bound of each
// objective is the last one accepted for it.
func TestTheLogHoldsEveryAcceptedDeclarationInOrder(t *testing.T) {
	objectives := []string{"tok_s", "ttft_ms", "tpot_ms", "nope"}
	for seed := range uint64(8) {
		r := rand.New(rand.NewPCG(seed, seed+1))
		w := newWorld(t)
		w.tasks.Put("/repo", commit, servingTask())
		a := w.newApp()
		ctx := ctxFor(t)
		var accepted []core.Bound
		for range 40 {
			s := statement(objectives[r.IntN(len(objectives))], float64(r.IntN(5))*100)
			c := core.CallContext{Scope: scope, Run: core.RunID([]string{"run-1", "run-2", ""}[r.IntN(3)]), Agent: []string{"a", "b"}[r.IntN(2)]}
			b, err := a.DeclareBound(ctx, c, commit, s)
			ok := s.Objective != "nope" && s.Value > 0
			if ok != (err == nil) {
				t.Fatalf("seed %d, %+v: accepted %v, want %v (%v)", seed, s, err == nil, ok, err)
			}
			if err == nil {
				accepted = append(accepted, b)
			}
		}
		got, err := a.Bounds(ctx, scope)
		if err != nil || !slices.Equal(got, accepted) {
			t.Fatalf("seed %d: Bounds = %+v, %v; want the accepted ones %+v", seed, got, err, accepted)
		}
		for i, b := range got {
			if b.Seq != uint64(i+1) {
				t.Fatalf("seed %d: bound %d has place %d", seed, i, b.Seq)
			}
		}
	}
}
