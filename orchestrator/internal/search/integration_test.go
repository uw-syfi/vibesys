package search_test

import (
	"context"
	"fmt"
	"math/rand/v2"
	"testing"
	"time"

	cli "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon/daemontest"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
	"google.golang.org/protobuf/types/known/timestamppb"
)

// Histories include shared jobs, new submitters, repeated finals, resets and daemon restarts.
func TestRandomJobHistoriesTellEveryLiveSubmitterExactlyOnce(t *testing.T) {
	for seed := range uint64(30) {
		random := rand.New(rand.NewPCG(seed, 42))
		rig := daemontest.NewRig(t)
		store := new(search.Store)
		rig.JobSource = search.Source{Store: store}
		var runs []core.RunID
		for i := range 2 {
			resp, err := rig.Service.Dispatch(context.Background(), &pb.DispatchRequest{Command: &pb.Command{Id: fmt.Sprint("start", i), Body: &pb.Command_StartRun{StartRun: &pb.StartRun{Repository: "/repo", Task: "task", FanoutLimit: 1, Lead: &pb.AgentSpec{Harness: pb.Harness_HARNESS_CLAUDE_CODE}}}}})
			if err != nil {
				t.Fatal(err)
			}
			runs = append(runs, core.RunID(resp.Receipt.RunId))
		}
		rig.Settle(t)
		fake := clidaemontest.New()
		session := new(clidaemon.Session)
		finals := map[string]bool{}
		for step := range 20 {
			job := fmt.Sprintf("j%d", random.IntN(5))
			if random.IntN(2) == 0 || step == 19 {
				finals[job] = true
			}
			var facts search.Facts
			for n := range 5 {
				id := fmt.Sprintf("j%d", n)
				state := cli.JobState_JOB_STATE_RUNNING
				if finals[id] {
					state = cli.JobState_JOB_STATE_SUCCEEDED
				}
				facts = append(facts, &cli.TaskFact{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: id, State: state, Submitters: []*cli.Submitter{{RunId: string(runs[0]), At: timestamppb.New(time.Unix(1, 0))}, {RunId: string(runs[1]), At: timestamppb.New(time.Unix(1, 0))}}}}})
			}
			fake.SetFacts(facts)
			session.Disconnected()
			stream, err := fake.WatchTask(context.Background(), search.Request("/repo", "task"))
			if err != nil {
				t.Fatal(err)
			}
			for {
				m, err := stream.Recv()
				if err != nil {
					break
				}
				changed, err := session.Apply(m)
				if err != nil {
					t.Fatal(err)
				}
				if changed {
					for _, run := range runs {
						store.Put(run, search.Fold(session.Facts()))
					}
				}
			}
			rig.Clock.Advance(daemontest.PollEvery)
			rig.Settle(t)
			if random.IntN(3) == 0 {
				rig.Restart(t)
				rig.Settle(t)
			}
		}
		for _, run := range runs {
			events, err := rig.Store.EventsAfter(run, 0, 1000)
			if err != nil {
				t.Fatal(err)
			}
			told := map[core.JobID]map[core.MessageID]bool{}
			for _, e := range events {
				if m, ok := e.Event.Body.(core.Message); ok {
					if j, ok := m.Body.(core.JobFinished); ok {
						if told[j.Job] == nil {
							told[j.Job] = map[core.MessageID]bool{}
						}
						told[j.Job][m.ID] = true
					}
				}
			}
			for job := range finals {
				if len(told[core.JobID(job)]) != 1 {
					t.Fatalf("seed %d run %s job %s: %v", seed, run, job, told)
				}
			}
		}
	}
}

func TestDisconnectedFollowerRetainsFactsAcrossReset(t *testing.T) {
	fake := clidaemontest.New()
	fake.SetFacts(search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", State: cli.JobState_JOB_STATE_SUCCEEDED}}}})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := new(search.Store)
	changes, sleeps := 0, 0
	follower := clidaemon.Follower{
		Connect: func(context.Context) (clidaemon.Connection, error) { return fake, nil },
		Bound: func(c context.Context) (context.Context, context.CancelFunc) {
			return context.WithDeadline(c, time.Unix(1<<40, 0))
		},
		Changed: func(f search.Facts) { changes++; store.Put("r", search.Fold(f)) },
		Failed: func(e *clidaemon.Unreadable) {
			v, _ := store.Get("r")
			v.Reason = search.StreamLost
			v.Detail = e.Detail
			store.Put("r", v)
		},
		Sleep: func(context.Context, time.Duration) error {
			sleeps++
			v, _ := store.Get("r")
			if len(v.Facts) != 1 || v.Reason != search.StreamLost || v.Detail == "" {
				t.Fatalf("lost retained view: %+v", v)
			}
			if sleeps == 2 {
				cancel()
				return context.Canceled
			}
			return nil
		},
	}
	if err := follower.Follow(ctx, search.Request("/repo", "task")); err != context.Canceled {
		t.Fatal(err)
	}
	if changes != 2 {
		t.Fatalf("resets applied %d times", changes)
	}
}
