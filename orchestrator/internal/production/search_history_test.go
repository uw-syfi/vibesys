package production

import (
	"context"
	"fmt"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math/rand/v2"
	"testing"
	"testing/synctest"
	"time"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// test-isolation: compose the private production adapter with the real follower,
// reconciler and daemon. Only durable events, not internal maps, are the oracle.
func TestRandomJobHistoriesTellEveryLiveSubmitterExactlyOnce(t *testing.T) {
	for seed := range uint64(30) {
		t.Run(fmt.Sprint(seed), func(t *testing.T) {
			synctest.Test(t, func(t *testing.T) {
				random := rand.New(rand.NewPCG(seed, 42))
				rig := daemontest.NewRig(t)
				var runs []core.RunID
				for i := range 2 {
					resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("start", i), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{Repository: fmt.Sprint("/repo-", i), Task: "task", FanoutLimit: 1, Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}}}}})
					if err != nil {
						t.Fatal(err)
					}
					runs = append(runs, core.RunID(resp.Receipt.RunId))
				}
				rig.Settle(t)
				// Each repository has a task store of its own, holding the jobs its one live run submitted.
				fakes := [2]*clidaemontest.Fake{clidaemontest.New(), clidaemontest.New()}
				offline := false
				var r *Reconciler
				var resume chan struct{}
				boot := func() {
					resume = make(chan struct{})
					r = &Reconciler{Store: new(search.Store)}
					r.Follow = func(ctx context.Context, k Subscription, publish func(search.View)) {
						f := clidaemon.Follower{Ensurer: clidaemontest.AnySocket{}, Connect: func(context.Context, clidaemon.Socket) (clidaemon.Connection, error) {
							if offline {
								return nil, &clidaemon.Unreadable{Reason: core.StreamLost, Detail: "offline"}
							}
							return fakes[map[string]int{"/repo-0": 0, "/repo-1": 1}[k.Repository]], nil
						}, Bound: func(c context.Context) (context.Context, context.CancelFunc) {
							return context.WithDeadline(c, time.Unix(1<<40, 0))
						}, Sleep: func(c context.Context, _ time.Duration) error {
							select {
							case <-c.Done():
								return c.Err()
							case <-resume:
								return nil
							}
						}}
						v := search.View{}
						followSearch(ctx, f, k, &v, publish)
					}
					rig.JobSource = search.Source{Store: r.Store}
				}
				boot()
				defer func() { r.Close() }()
				type job struct {
					state   cli.JobState
					subs    [2]bool
					present bool
				}
				jobs := [5]job{}
				expected := map[core.RunID]map[core.JobID]core.JobFinished{}
				for _, run := range runs {
					expected[run] = map[core.JobID]core.JobFinished{}
				}
				poll := func() { rig.Clock.Advance(daemontest.PollEvery); rig.Settle(t) }
				check := func() {
					for _, run := range runs {
						events, err := rig.Store.EventsAfter(run, 0, 10000)
						if err != nil {
							t.Fatal(err)
						}
						told := map[core.JobID]map[core.MessageID]core.JobFinished{}
						for _, e := range events {
							if m, ok := e.Event.Body.(core.Message); ok {
								if j, ok := m.Body.(core.JobFinished); ok {
									if told[j.Job] == nil {
										told[j.Job] = map[core.MessageID]core.JobFinished{}
									}
									told[j.Job][m.ID] = j
								}
							}
						}
						if len(told) != len(expected[run]) {
							t.Fatalf("seed %d run %s: got %v want %v", seed, run, told, expected[run])
						}
						for id, want := range expected[run] {
							if len(told[id]) != 1 {
								t.Fatalf("duplicate or missing tell %s: %v", id, told)
							}
							for _, got := range told[id] {
								if got.Outcome != want.Outcome || got.Detail != want.Detail {
									t.Fatalf("got %+v want %+v", got, want)
								}
							}
						}
					}
				}
				for step := range 50 {
					n := random.IntN(len(jobs))
					j := &jobs[n]
					switch random.IntN(8) {
					case 0:
						j.present = true
						j.subs[random.IntN(2)] = true // late join, even after final
					case 1, 2:
						j.present = true
						j.state = []cli.JobState{cli.JobState_JOB_STATE_RUNNING, cli.JobState_JOB_STATE_SUCCEEDED, cli.JobState_JOB_STATE_FAILED, cli.JobState_JOB_STATE_CANCELLED, cli.JobState_JOB_STATE_LOST}[random.IntN(5)]
					case 3:
						j.present = false // vanish and later reappear
					case 4:
						offline = true
					case 5:
						offline = false
					case 6:
						r.Close()
						boot()
						rig.Restart(t)
						poll()
						check() // volatile views do not survive restart
					case 7:
						if step > 25 {
							run := runs[random.IntN(2)]
							state, _ := rig.Daemon.State().Run(run)
							if state.Status.Live() {
								_, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("end", step), Body: &pb.Command_StopRun{StopRun: &pb.StopRun{RunId: string(run)}}}})
								if err != nil {
									t.Fatal(err)
								}
								rig.Settle(t)
								rig.Clock.Advance(core.StopGrace + time.Second)
								rig.Settle(t)
							}
						}
					}
					// Finish offline work and reconnect at the final step to establish eventuality.
					if step == 49 {
						offline = false
					}
					for store, fake := range fakes {
						var facts search.Facts
						for i, j := range jobs {
							if !j.present {
								continue
							}
							job := &cli.Job{Id: fmt.Sprintf("j%d", i), State: j.state, CancelReason: "user cancelled"}
							if j.subs[store] {
								job.Submitters = append(job.Submitters, &cli.Submitter{RunId: string(runs[store]), At: timestamppb.New(time.Unix(1, 0))})
							}
							facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: job}})
						}
						fake.SetFacts(facts)
					}
					live := rig.Daemon.State().Runs()
					r.Sync(context.Background(), live)
					synctest.Wait()
					// Release exactly one reconnect; Wait reaches the next injected Sleep.
					for range len(r.active) {
						resume <- struct{}{}
						synctest.Wait()
					}
					if !offline {
						for _, run := range live {
							if !run.Status.Live() {
								continue
							}
							for i, j := range jobs {
								idx := 0
								if run.ID == runs[1] {
									idx = 1
								}
								if !j.present || !j.subs[idx] {
									continue
								}
								var state core.JobState
								switch j.state {
								case cli.JobState_JOB_STATE_SUCCEEDED:
									state = core.JobSucceeded
								case cli.JobState_JOB_STATE_FAILED:
									state = core.JobFailed
								case cli.JobState_JOB_STATE_CANCELLED:
									state = core.JobCancelled
								case cli.JobState_JOB_STATE_LOST:
									state = core.JobLost
								default:
									continue
								}
								id := core.JobID(fmt.Sprintf("j%d", i))
								if _, ok := expected[run.ID][id]; !ok {
									detail := ""
									if state == core.JobCancelled {
										detail = "user cancelled"
									}
									expected[run.ID][id] = core.JobFinished{Job: id, Outcome: state, Detail: detail}
								}
							}
						}
					}
					poll()
					check()
				}
			})
		})
	}
}
