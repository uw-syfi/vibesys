package production

import (
	"context"
	"fmt"
	"math/rand/v2"
	"slices"
	"testing"
	"testing/synctest"
	"time"

	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/encoding/prototext"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// test-isolation: these compose the private Reconciler with the daemon rig, as search_ended_test.go does;
// the oracle is what a client is served for each run, which is all the desktop app reads of its search
// facts: the SearchState behind a report and a row's candidates, and the summary behind its best.

// route is how a run ends.
type route int

const (
	// stopped: the operator stops it.
	stopped route = iota
	// finished: the lead gives its final report and the run's work comes to rest.
	finished
	// stalled: the lead is reminded as often as it is and goes on doing nothing.
	stalled
	// leadGone: the lead's process is gone and the host finds it so.
	leadGone
	routes
)

func (r route) String() string { return [...]string{"stopped", "finished", "stalled", "lead gone"}[r] }

// stopReason is what the run says of why it ended.
var stopReason = map[route]core.RunStopReason{
	stopped: core.StopOperator, finished: core.StopFinished, stalled: core.StopStalled, leadGone: core.StopLeadGone,
}

// served is what a client of the host is told of one run.
type served struct {
	search  *pb.SearchState
	summary *pb.RunSummary
}

// servedRuns reads every run as the desktop app does: a snapshot of detail SUMMARY, and the search views
// and summaries that follow it.
func servedRuns(t *testing.T, rig *daemontest.Rig) map[core.RunID]*served {
	t.Helper()
	w, first, err := rig.Service.WatchRunsDetailFrom(core.Operator{}, 0, pb.SnapshotDetail_SNAPSHOT_DETAIL_SUMMARY)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	out := map[core.RunID]*served{}
	of := func(id string) *served {
		if out[core.RunID(id)] == nil {
			out[core.RunID(id)] = &served{}
		}
		return out[core.RunID(id)]
	}
	for _, frame := range first {
		if snapshot := client.WatchRunsSnapshot(frame); snapshot != nil {
			for _, summary := range snapshot.GetSummaries() {
				of(summary.GetRunId()).summary = summary
			}
		}
		if s := client.WatchRunsSearch(frame); s != nil {
			of(s.GetRunId()).search = s
		}
		if summary := client.WatchRunsSummary(frame); summary != nil {
			of(summary.GetRunId()).summary = summary
		}
	}
	return out
}

// shown is what a run showed the last time it was looked at.
type shown struct {
	facts   []string
	summary *pb.RunSummary
}

// best is the best the summary gives for an objective, or nil when it gives none.
func best(summary *pb.RunSummary, objective string) *float64 {
	for _, o := range summary.GetObjectives() {
		if o.GetName() == objective && o.BestValue != nil {
			return o.BestValue
		}
	}
	return nil
}

// shownOfServed is what the row and the report of a run show: the search view's facts, and the best of each
// objective the summary gives.
func shownOfServed(s *served) []string {
	out := shownOf(s.search)
	for _, o := range s.summary.GetObjectives() {
		out = append(out, "standing "+prototext.Format(o))
	}
	slices.Sort(out)
	return out
}

// leadSays posts a report as the lead of a run, with its own credentials, as its tool server does.
func leadSays(t *testing.T, rig *daemontest.Rig, lead core.Agent, kind pb.ReportKind) {
	t.Helper()
	token := rig.Sidecars.Session(core.Session{Agent: lead.ID, N: lead.Session}).Credentials.GetToken()
	ctx := metadata.NewIncomingContext(context.Background(), metadata.Pairs(
		daemon.MetaAgent, string(lead.ID), daemon.MetaSession, fmt.Sprint(lead.Session), daemon.MetaToken, token))
	resp, err := rig.AgentService.Dispatch(ctx, &pb.DispatchRequest{Command: &pb.Command{
		Id:   "report-" + string(lead.ID),
		Body: &pb.Command_PostReport{PostReport: &pb.PostReport{Report: &pb.Report{Kind: kind, Headline: "done"}}},
	}})
	if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
		t.Fatalf("the lead's report: %v, %v", resp, err)
	}
}

// A run that ends shows, in its report and its row, everything it showed while it lived and everything its
// task's job log held when it ended, however it ends: stopped by the operator, finished by its lead's final
// report, stalled, or failed with its lead gone. A repository holds one run at a time, so the runs of a task
// follow one another: each ends on a task whose earlier runs have ended, while another repository's run goes on.
//
// This held one fact the other way: the facts an ended run is shown came from one read of its task for the
// life of the host, taken when the first run of the task ended, so every later run of the task ended
// showing no measurement, no candidate, no best, no workstream and no idea.
func TestARunEndsShowingEverythingItShowedWhicheverWayItEnds(t *testing.T) {
	const runCount = 5
	// The runs of a repository follow one another, in this order.
	repository := [runCount]string{"/repo-a", "/repo-b", "/repo-a", "/repo-b", "/repo-a"}
	for seed := range thinnedUnderRace(24) {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				random := rand.New(rand.NewPCG(seed, 5))
				worlds := map[string]*taskWorld{"/repo-a": {fake: clidaemontest.New()}, "/repo-b": {fake: clidaemontest.New()}}
				rec := &Reconciler{Store: new(search.Store)}
				resume := make(chan struct{})
				rec.Follow = followOver(
					func(k Subscription) clidaemon.Connection { return worlds[k.Repository].fake },
					func() *clidaemon.Unreadable { return nil }, resume)
				defer rec.Close()

				rig := daemontest.NewRig(t)
				rig.Search, rig.JobSource = rec.Store, search.Source{Store: rec.Store}
				rig.Restart(t)

				// Each run ends by a route, some steps after it starts. A stall is the host's clock running
				// out on a lead that is reminded and does nothing, which no step brings about, so only a run
				// that nothing waits for ends that way, in the last phase.
				var how [runCount]route
				var lives [runCount]int
				var ids [runCount]core.RunID
				var startedAt [runCount]int
				started := [runCount]bool{}
				for i := range how {
					how[i], lives[i] = []route{stopped, finished, leadGone}[random.IntN(3)], 2+random.IntN(8)
				}
				how[runCount-1] = stalled // the last run of repository /repo-a: nothing waits for it.
				state := func(i int) core.Run {
					run, _ := rig.Daemon.State().Run(ids[i])
					return run
				}
				over := map[core.RunID][]*cli.TaskFact{}
				before := map[core.RunID]shown{}
				sync := func() {
					rec.Sync(context.Background(), rig.Daemon.State().Runs())
					synctest.Wait()
					for range len(rec.active) {
						resume <- struct{}{}
						synctest.Wait()
					}
					for i, id := range ids {
						if _, seen := over[id]; started[i] && !seen && state(i).Status.Final() {
							over[id] = slices.Clone(worlds[repository[i]].facts)
						}
					}
					rig.Clock.Advance(daemontest.PollEvery)
					rig.Settle(t)
				}
				check := func(step string) {
					t.Helper()
					all := servedRuns(t, rig)
					for i, id := range ids {
						if !started[i] {
							continue
						}
						got := all[id]
						if got == nil || got.search == nil || got.summary == nil {
							t.Fatalf("seed %d %s: run %d is not served whole: %+v", seed, step, i, got)
						}
						now := shownOfServed(got)
						if facts, isOver := over[id]; isOver {
							want := projectionOf(search.Fold(facts), id)
							if want, standing := shownOf(want), search.Standings(want); !slices.Equal(shownOf(got.search), want) || len(got.summary.GetObjectives()) != len(standing) {
								t.Fatalf("seed %d %s: run %d ended (%s) and is served %v, want what its task held when it ended: %v and %d standings", seed, step, i, how[i], now, want, len(standing))
							}
						} else if want := shownOf(projectionOf(search.Fold(worlds[repository[i]].facts), id)); !slices.Equal(shownOf(got.search), want) {
							t.Fatalf("seed %d %s: run %d is live and is served %v, its task holds %v", seed, step, i, now, want)
						}
						facts := shownOf(got.search)
						if lost := missing(before[id].facts, facts); len(lost) > 0 {
							t.Fatalf("seed %d %s: run %d (%s, ended by %s) showed %v before and no longer shows %v", seed, step, i, state(i).Status, how[i], before[id].facts, lost)
						}
						// The best only improves: "ops" is measured upward.
						if was, is := best(before[id].summary, "ops"), best(got.summary, "ops"); was != nil && (is == nil || *is < *was) {
							t.Fatalf("seed %d %s: run %d (%s, ended by %s) showed a best of %v before and shows %v", seed, step, i, state(i).Status, how[i], *was, is)
						}
						before[id] = shown{facts: facts, summary: got.summary}
					}
				}
				// startable says the run is next in its repository, which holds no run that is not over.
				startable := func(i int) bool {
					if started[i] {
						return false
					}
					for j := range i {
						if repository[j] == repository[i] && (!started[j] || !state(j).Status.Final()) {
							return false
						}
					}
					return true
				}
				end := func(i int) {
					lead, _ := rig.Daemon.State().Agent(state(i).Lead)
					switch how[i] {
					case stopped:
						if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("stop", i), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(ids[i])}}}}); err != nil {
							t.Fatal(err)
						}
					case finished:
						leadSays(t, rig, lead, pb.ReportKind_REPORT_KIND_FINAL)
					case leadGone:
						// A lead is given its session again each time it loses one, a few times in all.
						for range core.MaxLeadResumes + 2 {
							if state(i).Status.Final() {
								break
							}
							lead, _ = rig.Daemon.State().Agent(state(i).Lead)
							rig.Sidecars.Session(core.Session{Agent: lead.ID, N: lead.Session}).Die()
							rig.Restart(t)
							rig.Settle(t)
						}
					}
					rig.Settle(t)
					if !state(i).Status.Final() {
						rig.Clock.Advance(core.StopGrace + time.Second)
						rig.Settle(t)
					}
					if got, want := state(i).StopReason, stopReason[how[i]]; !state(i).Status.Final() || got != want {
						t.Fatalf("seed %d: run %d was to end %s and is %s for %q, want %q", seed, i, how[i], state(i).Status, got, want)
					}
				}

				for step := range 60 {
					for i := range ids {
						if startable(i) {
							resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("start", i), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{
								Repository: repository[i], Task: "task", FanoutLimit: 1, Brief: "go", Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE},
							}}}})
							if err != nil || resp.GetReceipt().GetRefusal() != pb.Refusal_REFUSAL_UNSPECIFIED {
								t.Fatalf("start %d: %v, %v", i, resp, err)
							}
							ids[i], started[i], startedAt[i] = core.RunID(resp.GetReceipt().GetRunId()), true, step
							rig.Settle(t)
						}
						if started[i] && how[i] != stalled && step == startedAt[i]+lives[i] && !state(i).Status.Final() {
							end(i)
						}
					}
					// A live run measures: it adds to its task's job log.
					if i := random.IntN(runCount); started[i] && !state(i).Status.Final() {
						worlds[repository[i]].record(random, ids[i], int64(10+step))
					}
					sync()
					check(fmt.Sprintf("step %d", step))
				}
				// The clock runs out on the run that is left.
				for range 8 {
					rig.Clock.Advance(core.LeadIdleAfter + time.Second)
					rig.Settle(t)
					sync()
				}
				check("the clock ran out")
				for i := range ids {
					if !started[i] {
						t.Fatalf("seed %d: run %d never started", seed, i)
					}
					if run := state(i); !run.Status.Final() || run.StopReason != stopReason[how[i]] {
						t.Fatalf("seed %d: run %d was to end %s and is %s for %q", seed, i, how[i], run.Status, run.StopReason)
					}
				}
			})
		})
	}
}
