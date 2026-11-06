package core_test

import (
	"fmt"
	"math/rand/v2"
	"reflect"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// A repository holds one run that is not over at a time, whatever its task, and a run that is still
// stopping holds it. The refusal names the run in the way, so a client can tell its user.
func TestARepositoryHoldsOneRunThatIsNotOver(t *testing.T) {
	w, run, leadID := started(t, 1, steers)
	for _, task := range []string{"task", "other-task"} {
		r := w.command(operator, core.StartRun{Repository: "repo", Task: task, Lead: lead, Brief: "go"})
		if r.Refusal != core.RefuseRepositoryInUse || r.Run != run {
			t.Fatalf("a second start of %q got refusal %q for run %q, want %q for %q", task, r.Refusal, r.Run, core.RefuseRepositoryInUse, run)
		}
		if len(w.effects) != 0 {
			t.Fatalf("a refused start asked for %v", w.effects)
		}
	}
	w.command(operator, core.StopRun{Run: run})
	if r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", Lead: lead, Brief: "go"}); r.Refusal != core.RefuseRepositoryInUse || r.Run != run {
		t.Fatalf("a start while the run is stopping got %+v", r)
	}
	if r := w.command(operator, core.StartRun{Repository: "elsewhere", Task: "task", Lead: lead, Brief: "go"}); r.Refusal != "" {
		t.Fatalf("a start in another repository was refused: %q", r.Refusal)
	}
	w.fact(leadID, core.SessionClosed{})
	if got, _ := w.s.Run(run); got.Status != core.RunEnded {
		t.Fatalf("run is %s, want ended", got.Status)
	}
	if r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", Lead: lead, Brief: "go"}); r.Refusal != "" {
		t.Fatalf("a start after the run ended was refused: %q", r.Refusal)
	}
}

// Whatever order starts, stops and endings come in, a start is refused exactly when a run that is not
// over works in its repository, and the refusal names that run.
func TestAStartIsRefusedExactlyWhileTheRepositoryHoldsALiveRun(t *testing.T) {
	repositories := []string{"repo-a", "repo-b", "repo-c"}
	for seed := range uint64(200) {
		r := rand.New(rand.NewPCG(seed, seed^0x9e3779b97f4a7c15))
		w := newScript(t)
		holder := map[string]core.RunID{} // The runs that are not over, by repository.
		leads := map[core.RunID]core.AgentID{}
		stopping := map[core.RunID]bool{}
		for range 60 {
			repository := repositories[r.IntN(len(repositories))]
			switch r.IntN(3) {
			case 0:
				receipt := w.command(operator, core.StartRun{Repository: repository, Task: fmt.Sprintf("task-%d", r.IntN(2)), Lead: lead, Brief: "go"})
				if busy, held := holder[repository]; held {
					if receipt.Refusal != core.RefuseRepositoryInUse || receipt.Run != busy {
						t.Fatalf("seed %d: start in %s got %+v, want refusal for %s", seed, repository, receipt, busy)
					}
					continue
				}
				if receipt.Refusal != "" {
					t.Fatalf("seed %d: start in a free repository %s refused: %q", seed, repository, receipt.Refusal)
				}
				holder[repository], leads[receipt.Run] = receipt.Run, receipt.Agent
				w.open(receipt.Agent, steers)
				w.endTurn(receipt.Agent, core.TurnCompleted)
			case 1:
				if run, held := holder[repository]; held {
					w.command(operator, core.StopRun{Run: run})
					stopping[run] = true
				}
			default:
				if run, held := holder[repository]; held && stopping[run] {
					w.fact(leads[run], core.SessionClosed{})
					delete(holder, repository)
				}
			}
			seen := map[string]bool{}
			for _, run := range w.s.Runs() {
				if run.Status.Final() {
					continue
				}
				if seen[run.Repository] {
					t.Fatalf("seed %d: two runs that are not over in %s", seed, run.Repository)
				}
				seen[run.Repository] = true
			}
		}
	}
}

// A log written before the rule can hold two runs that are not over in one repository. Replaying it
// leaves both as they were, they can still be stopped, and a new start is refused.
func TestReplayingTwoLiveRunsInOneRepositoryChangesNothing(t *testing.T) {
	w := newScript(t)
	var log []core.Event
	for i, id := range []core.RunID{"run-old", "run-new"} {
		agent := core.AgentID(fmt.Sprintf("agent-%d", i))
		at := epoch.Add(time.Duration(i) * time.Minute)
		log = append(log,
			core.Event{Run: id, At: at, Body: core.Run{ID: id, Repository: "repo", Task: "task", Status: core.RunRunning, Lead: agent, CreatedAt: at}},
			core.Event{Run: id, At: at, Body: core.Agent{ID: agent, Run: id, Role: core.RoleLead, Status: core.AgentLaunching, Session: 1, StartedAt: at}})
	}
	w.s = core.Replay(core.State{}, log)
	if got := w.s.Runs(); len(got) != 2 || got[0].Status != core.RunRunning || got[1].Status != core.RunRunning {
		t.Fatalf("replay changed the runs: %+v", got)
	}
	if again := core.Replay(core.State{}, log); !reflect.DeepEqual(again, w.s) {
		t.Fatalf("replay is not repeatable")
	}
	if r := w.command(operator, core.StartRun{Repository: "repo", Task: "task", Lead: lead, Brief: "go"}); r.Refusal != core.RefuseRepositoryInUse || r.Run != "run-old" {
		t.Fatalf("a start got %+v, want a refusal naming the older run", r)
	}
	if r := w.command(operator, core.StopRun{Run: "run-new"}); r.Refusal != "" {
		t.Fatalf("stopping a run of the old log was refused: %q", r.Refusal)
	}
}
