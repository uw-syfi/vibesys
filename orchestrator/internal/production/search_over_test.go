package production

import (
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"testing/synctest"
	"time"

	"google.golang.org/protobuf/encoding/prototext"
	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// test-isolation: these compose the private Reconciler with the job service's Fake, as
// search_ended_test.go does; the oracle is the SearchState the host derives for each run, which is what
// every screen of the desktop app (a run's report, its row, its Agents, Cost and Catch me up) is made of.

// taskWorld is the job service's side of one task: the facts it holds, which every run of the task adds to.
type taskWorld struct {
	fake  *clidaemontest.Fake
	facts []*cli.TaskFact
	// jobs, plans and workstreams count what has been recorded, for numbers that are never reused.
	jobs, plans, workstreams int
}

func (w *taskWorld) put(facts ...*cli.TaskFact) {
	w.facts = append(w.facts, facts...)
	w.fake.SetFacts(w.facts)
}

// record adds to the task what a run does in one step, as the CLI records it: a measured candidate (a
// job and its result), and now and then a plan in a workstream of its own. at is when it happens.
func (w *taskWorld) record(random *rand.Rand, run core.RunID, at int64) {
	stamp := timestamppb.New(time.Unix(at, 0))
	origin := &cli.RecordOrigin{RunId: string(run), Agent: "agent", At: stamp}
	w.jobs++
	job := fmt.Sprintf("job-%03d", w.jobs)
	commit := strings.Repeat(string(rune('a'+random.IntN(6))), 40)
	w.put(
		&cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{
			Id: job, State: cli.JobState_JOB_STATE_SUCCEEDED,
			Submitters: []*cli.Submitter{{RunId: string(run), At: stamp}}, SubmittedAt: stamp,
		}}},
		&cli.TaskFact{Fact: &cli.TaskFact_Result{Result: &cli.Result{
			JobId: job, Commit: commit, RecordedAt: stamp,
			Outcome: &cli.Result_Measure{Measure: &cli.MeasureResult{
				Metric: &cli.Metric{Name: "ops", Direction: cli.Direction_DIRECTION_MAXIMIZE}, Value: float64(1 + random.IntN(1000)),
			}},
		}}},
	)
	if random.IntN(3) == 0 {
		w.workstreams++
		w.plans++
		w.put(
			&cli.TaskFact{Fact: &cli.TaskFact_Workstream{Workstream: &cli.Workstream{
				Number: uint32(w.workstreams), Title: fmt.Sprintf("line %d", w.workstreams),
				Status: cli.WorkstreamStatus_WORKSTREAM_STATUS_ACTIVE, Origin: origin, UpdatedAt: stamp,
			}}},
			&cli.TaskFact{Fact: &cli.TaskFact_Plan{Plan: &cli.Plan{
				Number: uint32(w.plans), Title: fmt.Sprintf("idea %d", w.plans), Status: cli.PlanStatus_PLAN_STATUS_OPEN,
				CreatedAt: stamp, UpdatedAt: stamp, Workstream: uint32(w.workstreams), Origin: origin,
				Transitions: []*cli.PlanTransition{{Status: cli.PlanStatus_PLAN_STATUS_OPEN, Origin: origin}},
			}}},
		)
	}
}

// shownOf is everything a run's SearchState shows, as one sorted list of what each thing is: the jobs and
// results behind the candidates, the points of every objective's chart and the best marks among them, the
// ideas and workstreams, and the promotions. Two states show the same exactly when the lists are equal, and
// what one shows that another does not is a fact it lost.
func shownOf(s *pb.SearchState) []string {
	var out []string
	for _, j := range s.GetJobs() {
		out = append(out, "job "+j.GetId())
	}
	for _, r := range s.GetResults() {
		out = append(out, "result "+r.GetJobId())
	}
	for _, series := range s.GetSeries() {
		for _, p := range series.GetPoints() {
			out = append(out, fmt.Sprintf("point %s %s", series.GetObjective(), prototext.Format(p)))
		}
	}
	for _, i := range s.GetIdeas() {
		out = append(out, fmt.Sprintf("idea %d", i.GetPlanNumber()))
	}
	for _, w := range s.GetWorkstreamViews() {
		out = append(out, "workstream "+prototext.Format(w))
	}
	for _, p := range s.GetPromotions() {
		out = append(out, fmt.Sprintf("promotion %d", p.GetSeq()))
	}
	slices.Sort(out)
	return out
}

// projectionOf is the SearchState the host derives from a view for a run.
func projectionOf(v search.View, run core.RunID) *pb.SearchState {
	return search.Cap(v, &pb.SearchState{RunId: string(run)})
}

// missing is what before shows and after does not.
func missing(before, after []string) []string {
	var out []string
	for _, line := range before {
		if !slices.Contains(after, line) {
			out = append(out, line)
		}
	}
	return out
}

// A run that is over shows everything its task's job log held when it was seen over, and everything it
// showed while it lived, however the runs of a task are ended and in whatever order: the runs of a task
// follow one another (a repository holds one run that is not over), stop on their way to ending, end while
// another task's run goes on, and the host restarts between them. A run is never given the view of an
// earlier run's end.
//
// The world is generated from a seed. Every run of a task adds to the task's job log while it is live or
// stopping; a run that is over adds nothing. After every step each run's view is held to:
//   - not over: the task's job log as the service holds it now;
//   - over, read while the service answered: the task's job log as it was when the host first saw the run
//     over, and never less than the run showed before;
//   - over, read while the service did not answer: what the run showed before, with the reason.
func TestARunThatIsOverShowsEverythingItsTaskHeldWhenItEnded(t *testing.T) {
	// The runs of a repository follow one another, in this order.
	repository := [...]string{"/repo-a", "/repo-b", "/repo-a", "/repo-b", "/repo-a", "/repo-a"}
	runCount := len(repository)
	for seed := range thinnedUnderRace(60) {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				random := rand.New(rand.NewPCG(seed, 11))
				// Later runs of a task start after earlier ones ended: the case a real host is in.
				worlds := map[string]*taskWorld{"/repo-a": {fake: clidaemontest.New()}, "/repo-b": {fake: clidaemontest.New()}}
				all := make([]core.Run, runCount)
				for i := range all {
					all[i] = core.Run{ID: core.RunID(fmt.Sprintf("run-%d", i)), Repository: repository[i], Task: "task"}
				}
				// started says which runs the host knows of.
				started := make([]bool, runCount)
				runs := func() []core.Run {
					var out []core.Run
					for i := range all {
						if started[i] {
							out = append(out, all[i])
						}
					}
					return out
				}
				// startable says the run is next in its repository, which holds no run that is not over.
				startable := func(i int) bool {
					if started[i] {
						return false
					}
					for j := range i {
						if repository[j] == repository[i] && (!started[j] || !all[j].Status.Final()) {
							return false
						}
					}
					return true
				}
				var rec *Reconciler
				var resume chan struct{}
				down := false
				boot := func() {
					resume = make(chan struct{})
					rec = &Reconciler{Store: new(search.Store)}
					rec.Follow = followOver(
						func(k Subscription) clidaemon.Connection { return worlds[k.Repository].fake },
						func() *clidaemon.Unreadable {
							if down {
								return &clidaemon.Unreadable{Reason: core.NoDaemon, Detail: "no job service"}
							}
							return nil
						}, resume)
				}
				boot()
				defer func() { rec.Close() }()

				// For each run: the facts of its task when this host first saw it over, whether that read
				// could be made, and what it showed the last time it was looked at.
				type seenOver struct {
					facts    []*cli.TaskFact
					answered bool
				}
				over := map[core.RunID]seenOver{}
				shown := map[core.RunID][]string{}
				at := int64(1)
				sync := func() {
					rec.Sync(context.Background(), runs())
					synctest.Wait()
					for range len(rec.active) {
						resume <- struct{}{}
						synctest.Wait()
					}
					for _, run := range runs() {
						if _, seen := over[run.ID]; !seen && run.Status.Final() {
							over[run.ID] = seenOver{facts: slices.Clone(worlds[run.Repository].facts), answered: !down}
						}
					}
				}
				check := func(step string) {
					for _, run := range runs() {
						v, rev := rec.Store.Get(run.ID)
						if rev == 0 {
							t.Fatalf("seed %d %s: run %s has no view", seed, step, run.ID)
						}
						got := projectionOf(v, run.ID)
						now := shownOf(got)
						world := worlds[run.Repository]
						switch seen, isOver := over[run.ID]; {
						case !isOver:
							if want := shownOf(projectionOf(search.Fold(world.facts), run.ID)); !down && !slices.Equal(now, want) {
								t.Fatalf("seed %d %s: run %s is not over and shows %v, the task holds %v", seed, step, run.ID, now, want)
							}
						case seen.answered:
							want := shownOf(projectionOf(search.Fold(seen.facts), run.ID))
							if v.Reason != core.Readable || !slices.Equal(now, want) {
								t.Fatalf("seed %d %s: run %s is over (%s) and shows %v with reason %v, want what its task held when it ended: %v", seed, step, run.ID, run.Status, now, v.Reason, want)
							}
						default:
							if v.Reason == core.Readable {
								t.Fatalf("seed %d %s: run %s was read while the job service did not answer, yet is readable", seed, step, run.ID)
							}
						}
						if lost := missing(shown[run.ID], now); len(lost) > 0 {
							t.Fatalf("seed %d %s: run %s (%s) showed %v before and no longer shows %v", seed, step, run.ID, run.Status, shown[run.ID], lost)
						}
						shown[run.ID] = now
					}
				}
				sync()
				check("start")
				for step := range 80 {
					name := fmt.Sprintf("step %d", step)
					for i := range all {
						if startable(i) && random.IntN(3) > 0 {
							started[i], all[i].Status = true, core.RunRunning
						}
					}
					i := random.IntN(runCount)
					run := &all[i]
					switch random.IntN(8) {
					case 0, 1, 2:
						// A run that is not over works: it adds to its task's job log.
						if started[i] && !run.Status.Final() {
							at++
							worlds[run.Repository].record(random, run.ID, at)
						}
					case 3:
						// A run begins to stop. Its agents may still record a result before the last ends.
						if started[i] && run.Status == core.RunRunning {
							run.Status = core.RunStopping
						}
					case 4:
						// A run ends, from running or from stopping, ended or failed.
						if started[i] && !run.Status.Final() {
							run.Status = core.RunEnded
							if random.IntN(3) == 0 {
								run.Status = core.RunFailed
							}
						}
					case 5:
						// A stopping run records one more result as its last agent ends, and then it ends.
						if started[i] && run.Status == core.RunStopping {
							at++
							worlds[run.Repository].record(random, run.ID, at)
							run.Status = core.RunEnded
						}
					case 6:
						// The host restarts, with the job service answering or not for its whole life.
						rec.Close()
						down = random.IntN(4) == 0
						boot()
						// A restarted host holds no view and no memory of what it read.
						shown = map[core.RunID][]string{}
						over = map[core.RunID]seenOver{}
					case 7:
						down = false
					}
					sync()
					check(name)
				}
				// Every run starts and ends, and the host restarts a last time with the service answering.
				for range runCount {
					for i := range all {
						if startable(i) {
							started[i], all[i].Status = true, core.RunRunning
						}
						if started[i] && !all[i].Status.Final() {
							all[i].Status = core.RunEnded
						}
					}
					sync()
					check("every run ended")
				}
				rec.Close()
				down = false
				boot()
				shown, over = map[core.RunID][]string{}, map[core.RunID]seenOver{}
				sync()
				check("restart")
			})
		})
	}
}

// The failure of a live run: a run that started after an earlier run of its task ended, and ended in its
// turn, was given the read made when the earlier run ended, which holds nothing the later run did, so its
// report showed no measurement, workstream or idea. The property above covers the class; this is the case.
func TestARunThatStartsAfterAnEarlierRunEndedShowsItsOwnFactsWhenItEnds(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		world := &taskWorld{fake: clidaemontest.New()}
		resume := make(chan struct{})
		rec := &Reconciler{Store: new(search.Store)}
		rec.Follow = followOver(func(Subscription) clidaemon.Connection { return world.fake }, func() *clidaemon.Unreadable { return nil }, resume)
		defer rec.Close()
		random := rand.New(rand.NewPCG(1, 1))
		first := core.Run{ID: "first", Repository: "/repo", Task: "task", Status: core.RunRunning}
		world.record(random, first.ID, 1)
		rec.Sync(context.Background(), []core.Run{first})
		synctest.Wait()
		first.Status = core.RunEnded
		rec.Sync(context.Background(), []core.Run{first})
		synctest.Wait()

		second := core.Run{ID: "second", Repository: "/repo", Task: "task", Status: core.RunRunning}
		runs := []core.Run{first, second}
		rec.Sync(context.Background(), runs)
		synctest.Wait()
		world.record(random, second.ID, 2)
		world.record(random, second.ID, 3)
		resume <- struct{}{}
		synctest.Wait()
		second.Status = core.RunEnded
		runs[1] = second
		rec.Sync(context.Background(), runs)
		synctest.Wait()

		v, _ := rec.Store.Get("second")
		got, want := shownOf(projectionOf(v, "second")), shownOf(projectionOf(search.Fold(world.facts), "second"))
		if len(want) == 0 || !slices.Equal(got, want) {
			t.Fatalf("the later run shows %v when it ends, want what its task holds: %v", got, want)
		}
	})
}

// A run that is stopping is not over: its agents can still record a result as they stop, and the view the
// run is left with has it.
func TestAStoppingRunsLastResultIsInTheViewItEndsWith(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		world := &taskWorld{fake: clidaemontest.New()}
		resume := make(chan struct{})
		rec := &Reconciler{Store: new(search.Store)}
		rec.Follow = followOver(func(Subscription) clidaemon.Connection { return world.fake }, func() *clidaemon.Unreadable { return nil }, resume)
		defer rec.Close()
		random := rand.New(rand.NewPCG(2, 2))
		run := core.Run{ID: "run", Repository: "/repo", Task: "task", Status: core.RunRunning}
		world.record(random, run.ID, 1)
		rec.Sync(context.Background(), []core.Run{run})
		synctest.Wait()
		run.Status = core.RunStopping
		rec.Sync(context.Background(), []core.Run{run})
		synctest.Wait()
		world.record(random, run.ID, 2) // the last result, as the last agent ends
		run.Status = core.RunEnded
		rec.Sync(context.Background(), []core.Run{run})
		synctest.Wait()

		v, _ := rec.Store.Get("run")
		got, want := shownOf(projectionOf(v, "run")), shownOf(projectionOf(search.Fold(world.facts), "run"))
		if !slices.Equal(got, want) {
			t.Fatalf("the run ends showing %v, want %v", got, want)
		}
	})
}
