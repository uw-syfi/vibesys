package production

import (
	"context"
	"fmt"
	"math/rand/v2"
	"sync/atomic"
	"testing"
	"testing/synctest"
	"time"

	"google.golang.org/protobuf/types/known/timestamppb"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// test-isolation: these compose the private Reconciler and followSearch with the daemon rig, the
// way search_history_test.go does; the oracle is the SearchState a client is sent.

// followOver builds a Follow over fakes: connect answers with fake(k), or fails with err() when it
// returns one. Sleep waits for resume or the end of the context, so a test paces reconnects.
func followOver(fake func(Subscription) clidaemon.Connection, err func() *clidaemon.Unreadable, resume <-chan struct{}) func(context.Context, Subscription, func(search.View)) {
	return func(ctx context.Context, k Subscription, publish func(search.View)) {
		f := clidaemon.Follower{
			Ensurer: clidaemontest.AnySocket{},
			Connect: func(context.Context, clidaemon.Socket) (clidaemon.Connection, error) {
				if e := err(); e != nil {
					return nil, e
				}
				return fake(k), nil
			},
			Bound: func(c context.Context) (context.Context, context.CancelFunc) {
				return context.WithDeadline(c, time.Unix(1<<40, 0))
			},
			Sleep: func(c context.Context, _ time.Duration) error {
				select {
				case <-c.Done():
					return c.Err()
				case <-resume:
					return nil
				}
			},
		}
		v := search.View{}
		followSearch(ctx, f, k, &v, publish)
	}
}

func endedJob(id string, state cli.JobState, run core.RunID) *cli.TaskFact {
	return &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, State: state,
		Submitters: []*cli.Submitter{{RunId: string(run), At: timestamppb.New(time.Unix(1, 0))}}}}}
}

// counts is what a client sees of one run's search view.
type counts struct {
	finished int
	reason   pb.UnreadableReason // zero when readable
	readable bool
}

func finishedJobs(s *pb.SearchState) counts {
	c := counts{readable: s.GetHealth().GetLiveSince() != nil, reason: s.GetHealth().GetUnreadable().GetReason()}
	for _, j := range s.GetJobs() {
		if j.GetState() == cli.JobState_JOB_STATE_SUCCEEDED {
			c.finished++
		}
	}
	return c
}

// seen reads every run's search view as a client connecting now would.
func seen(t *testing.T, rig *daemontest.Rig) map[core.RunID]counts {
	t.Helper()
	w, first, err := rig.Service.WatchRunsFrom(core.Operator{}, 0)
	if err != nil {
		t.Fatal(err)
	}
	defer w.Close()
	out := map[core.RunID]counts{}
	for _, s := range client.WatchRunsSnapshot(first[0]).GetSearches() {
		out[core.RunID(s.GetRunId())] = finishedJobs(s)
	}
	return out
}

// An ended run's counts survive a restart of the host, and a view that says it is being read ends,
// whatever the order in which runs end and the host restarts, and whether the job service answers
// at the restart. A run's finished count is what its task holds when the host reads it, never less
// than a count already seen, and equal to the truth whenever the service answered.
func TestEndedRunsKeepTheirCountsAcrossRestartsInAnyOrder(t *testing.T) {
	const runCount = 3
	for seed := range thinnedUnderRace(40) {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				random := rand.New(rand.NewPCG(seed, 7))
				rig := daemontest.NewRig(t)
				var runs [runCount]core.RunID
				var fakes [runCount]*clidaemontest.Fake
				finished := [runCount]int{}
				for i := range runCount {
					resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("start", i), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{Repository: fmt.Sprint("/repo-", i), Task: "task", FanoutLimit: 1, Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}}}}})
					if err != nil {
						t.Fatal(err)
					}
					runs[i], fakes[i] = core.RunID(resp.Receipt.RunId), clidaemontest.New()
				}
				rig.Settle(t)
				repoOf := map[string]int{"/repo-0": 0, "/repo-1": 1, "/repo-2": 2}

				var rec *Reconciler
				var resume chan struct{}
				down := false // the job service does not answer in this host's life
				// readOnline[i] says the host that now runs read run i's task while the service answered.
				var readOnline [runCount]bool
				var read [runCount]bool
				boot := func() {
					resume = make(chan struct{})
					rec = &Reconciler{Store: new(search.Store)}
					rec.Follow = followOver(
						func(k Subscription) clidaemon.Connection { return fakes[repoOf[k.Repository]] },
						func() *clidaemon.Unreadable {
							if down {
								return &clidaemon.Unreadable{Reason: core.NoDaemon, Detail: "no job service"}
							}
							return nil
						}, resume)
					rig.JobSource = search.Source{Store: rec.Store}
					read = [runCount]bool{}
				}
				attach := func() { rig.Daemon.Search = rec.Store }
				boot()
				attach()
				defer func() { rec.Close() }()

				live := func(i int) bool {
					r, _ := rig.Daemon.State().Run(runs[i])
					return r.Status.Live()
				}
				last := [runCount]counts{}
				sync := func() {
					for i := range runCount {
						var facts search.Facts
						for n := range finished[i] {
							facts = append(facts, endedJob(fmt.Sprint("j", n), cli.JobState_JOB_STATE_SUCCEEDED, runs[i]))
						}
						fakes[i].SetFacts(facts)
					}
					rec.Sync(context.Background(), rig.Daemon.State().Runs())
					synctest.Wait()
					for range len(rec.active) {
						resume <- struct{}{}
						synctest.Wait()
					}
					for i := range runCount {
						if !live(i) && !read[i] {
							read[i], readOnline[i] = true, !down
						}
					}
					rig.Clock.Advance(daemontest.PollEvery)
					rig.Settle(t)
				}
				check := func(step string) {
					views := seen(t, rig)
					for i := range runCount {
						got, ok := views[runs[i]]
						if !ok {
							t.Fatalf("seed %d %s: run %d has no search view", seed, step, i)
						}
						if live(i) {
							continue
						}
						if got.reason == pb.UnreadableReason_UNREADABLE_REASON_NO_DAEMON {
							t.Fatalf("seed %d %s: ended run %d is still loading", seed, step, i)
						}
						if readOnline[i] && (!got.readable || got.finished != finished[i]) {
							t.Fatalf("seed %d %s: ended run %d shows %+v, want readable with %d finished", seed, step, i, got, finished[i])
						}
						if !readOnline[i] && got.readable {
							t.Fatalf("seed %d %s: run %d was read while the service did not answer, yet shows %+v", seed, step, i, got)
						}
						if got.readable {
							if got.finished < last[i].finished {
								t.Fatalf("seed %d %s: run %d count went from %d to %d", seed, step, i, last[i].finished, got.finished)
							}
							last[i] = got
						}
					}
				}
				stop := func(i int) {
					if !live(i) {
						return
					}
					if _, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("end", i), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(runs[i])}}}}); err != nil {
						t.Fatal(err)
					}
					rig.Settle(t)
					rig.Clock.Advance(core.StopGrace + time.Second)
					rig.Settle(t)
				}
				for step := range 40 {
					i := random.IntN(runCount)
					name := fmt.Sprint("step ", step)
					switch random.IntN(5) {
					case 0, 1:
						if live(i) {
							finished[i]++ // a live run's jobs finish; an ended run's task does not change
						}
					case 2:
						stop(i)
					case 3:
						// The host restarts, and the service is down for this host's whole life or not.
						rec.Close()
						down = random.IntN(4) == 0
						boot()
						rig.Restart(t)
						attach()
					case 4:
						// A run ends while no host is up to see it: the reconciler is gone and a new one
						// starts over what the old left.
						rec.Close()
						stop(i)
						down = random.IntN(4) == 0
						boot()
						rig.Restart(t)
						attach()
					}
					sync()
					check(name)
				}
				// Every run ends and the host restarts a last time with the service answering: all counts are final.
				for i := range runCount {
					stop(i)
				}
				rec.Close()
				down = false
				boot()
				rig.Restart(t)
				attach()
				sync()
				check("final")
				rec.Close()
			})
		})
	}
}

// The failure the property found first, kept as a plain case: a run ends, the host restarts, and a
// client asks. Before the fix the view was "loading" for as long as that host lived.
func TestAnEndedRunIsNotLoadingAfterTheHostRestarts(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		fake := clidaemontest.New()
		fake.SetFacts([]*cli.TaskFact{endedJob("j1", cli.JobState_JOB_STATE_SUCCEEDED, "r"), endedJob("j2", cli.JobState_JOB_STATE_FAILED, "r")})
		rec := &Reconciler{Store: new(search.Store)}
		rec.Follow = followOver(func(Subscription) clidaemon.Connection { return fake }, func() *clidaemon.Unreadable { return nil }, nil)
		defer rec.Close()
		rec.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunEnded, Repository: "/repo", Task: "task"}})
		synctest.Wait()
		v, _ := rec.Store.Get("r")
		if v.Reason != core.Readable || len(v.Facts) != 2 {
			t.Fatalf("view %+v, want readable with both jobs", v)
		}
	})
}

// A host reads an ended run's task once, however many times it is asked, and the answer stays.
func TestAnEndedRunIsReadOncePerHost(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		var calls atomic.Int32
		rec := &Reconciler{Store: new(search.Store), Follow: func(_ context.Context, _ Subscription, publish func(search.View)) {
			calls.Add(1)
			publish(search.View{Facts: search.Facts{endedJob("j", cli.JobState_JOB_STATE_SUCCEEDED, "a")}})
		}}
		defer rec.Close()
		runs := []core.Run{
			{ID: "a", Status: core.RunEnded, Repository: "/repo", Task: "task"},
			{ID: "b", Status: core.RunFailed, Repository: "/repo", Task: "task"},
			{ID: "c", Status: core.RunFailed, Repository: "/other", Task: "task"},
		}
		for range 5 {
			rec.Sync(context.Background(), runs)
			synctest.Wait()
		}
		if calls.Load() != 2 {
			t.Fatalf("read %d times, want once for each of the two tasks", calls.Load())
		}
		for _, id := range []core.RunID{"a", "b", "c"} {
			if v, _ := rec.Store.Get(id); v.Reason != core.Readable || len(v.Facts) != 1 {
				t.Fatalf("run %s: %+v", id, v)
			}
		}
	})
}

// A run that was live keeps what its live view knew when the last read cannot be made, and says why.
func TestARunThatEndsKeepsItsFactsWhenTheFinalReadFails(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		fake := clidaemontest.New()
		fake.SetFacts([]*cli.TaskFact{endedJob("j1", cli.JobState_JOB_STATE_SUCCEEDED, "r")})
		down := false
		resume := make(chan struct{})
		rec := &Reconciler{Store: new(search.Store)}
		rec.Follow = followOver(func(Subscription) clidaemon.Connection { return fake }, func() *clidaemon.Unreadable {
			if down {
				return &clidaemon.Unreadable{Reason: core.NoDaemon, Detail: "gone"}
			}
			return nil
		}, resume)
		defer rec.Close()
		run := core.Run{ID: "r", Status: core.RunRunning, Repository: "/repo", Task: "task"}
		rec.Sync(context.Background(), []core.Run{run})
		synctest.Wait()
		down, run.Status = true, core.RunEnded
		rec.Sync(context.Background(), []core.Run{run})
		synctest.Wait()
		v, _ := rec.Store.Get("r")
		if v.Reason != core.NoDaemon || len(v.Facts) != 1 {
			t.Fatalf("view %+v, want the facts it had and the reason the read failed", v)
		}
	})
}

// Every way the job service can fail ends the view with that reason, and none is "being read".
func TestEveryFailureToReadAnEndedRunSettlesAsItsOwnReason(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		for _, reason := range []core.ViewReason{core.NoDaemon, core.StartFailed, core.CLINotInstalled, core.ClientTooOld, core.DaemonTooOld, core.StreamLost, core.MessageTooLarge} {
			rec := &Reconciler{Store: new(search.Store)}
			rec.Follow = followOver(nil, func() *clidaemon.Unreadable { return &clidaemon.Unreadable{Reason: reason, Detail: "x"} }, nil)
			rec.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunEnded, Repository: "/repo", Task: "task"}})
			synctest.Wait()
			if v, _ := rec.Store.Get("r"); v.Reason != reason {
				t.Fatalf("reason %v settled as %v", reason, v.Reason)
			}
			rec.Close()
		}
	})
}

// A daemon of another build refuses the handshake; the read says so instead of waiting.
func TestAnEndedRunReadByADaemonOfAnotherBuildSettlesAsTooOld(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		fake := clidaemontest.New()
		fake.Versions.MinProtocolVersion = clidaemon.RequiredDaemonProtocol + 1
		fake.Versions.ProtocolVersion = clidaemon.RequiredDaemonProtocol + 1
		rec := &Reconciler{Store: new(search.Store)}
		rec.Follow = followOver(func(Subscription) clidaemon.Connection { return fake }, func() *clidaemon.Unreadable { return nil }, nil)
		defer rec.Close()
		rec.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunEnded, Repository: "/repo", Task: "task"}})
		synctest.Wait()
		if v, _ := rec.Store.Get("r"); v.Reason != core.ClientTooOld {
			t.Fatalf("view %+v, want client too old", v)
		}
	})
}

// A run that never opened has no jobs and is not read: it shows nothing finished, readable.
func TestARunThatNeverOpenedIsReadableWithNoJobsAndNeedsNoRead(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		rec := &Reconciler{Store: new(search.Store), Follow: func(context.Context, Subscription, func(search.View)) {
			t.Error("a start that never ran was read")
		}}
		defer rec.Close()
		rec.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunFailed, NeverOpened: true, Repository: "/repo", Task: "task"}})
		synctest.Wait()
		if v, rev := rec.Store.Get("r"); rev == 0 || v.Reason != core.Readable || len(v.Facts) != 0 {
			t.Fatalf("view %+v revision %d", v, rev)
		}
	})
}

// A run with no jobs at all reads as readable and empty, not as loading.
func TestAnEndedRunWithNoJobsIsReadableAndEmpty(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		rec := &Reconciler{Store: new(search.Store)}
		rec.Follow = followOver(func(Subscription) clidaemon.Connection { return clidaemontest.New() }, func() *clidaemon.Unreadable { return nil }, nil)
		defer rec.Close()
		rec.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunEnded, Repository: "/repo", Task: "task"}})
		synctest.Wait()
		if v, _ := rec.Store.Get("r"); v.Reason != core.Readable || len(v.Facts) != 0 {
			t.Fatalf("view %+v", v)
		}
	})
}

// A job service that accepts the connection and never sends: the read ends at its bound.
func TestAReadThatNeverAnswersEndsAtItsBound(t *testing.T) {
	synctest.Test(t, func(t *testing.T) {
		rec := &Reconciler{
			Store: new(search.Store),
			ReadBound: func(c context.Context) (context.Context, context.CancelFunc) {
				return context.WithTimeout(c, time.Minute)
			},
			Follow: func(ctx context.Context, _ Subscription, _ func(search.View)) { <-ctx.Done() },
		}
		defer rec.Close()
		rec.Sync(context.Background(), []core.Run{{ID: "r", Status: core.RunEnded, Repository: "/repo", Task: "task"}})
		synctest.Wait()
		if v, _ := rec.Store.Get("r"); v.Reason != core.Connecting {
			t.Fatalf("view %+v, want still being read", v)
		}
		time.Sleep(time.Minute + time.Second)
		synctest.Wait()
		if v, _ := rec.Store.Get("r"); v.Reason != core.StreamLost {
			t.Fatalf("view %+v, want settled as unreadable", v)
		}
	})
}
