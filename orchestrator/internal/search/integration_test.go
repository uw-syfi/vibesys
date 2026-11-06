package search_test

import (
	"context"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"

	"testing"
	"time"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"

	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon"
	"github.com/uw-syfi/vibesys/orchestrator/internal/clidaemon/clidaemontest"

	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

func TestDisconnectedFollowerRetainsFactsAcrossReset(t *testing.T) {
	fake := clidaemontest.New()
	fake.SetFacts(search.Facts{{Fact: &cli.TaskFact_Job{Job: &cli.Job{Id: "j1", State: cli.JobState_JOB_STATE_SUCCEEDED}}}})
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	store := new(search.Store)
	changes, sleeps := 0, 0
	follower := clidaemon.Follower{
		Ensurer: clidaemontest.AnySocket{},
		Connect: func(context.Context, clidaemon.Socket) (clidaemon.Connection, error) { return fake, nil },
		Bound: func(c context.Context) (context.Context, context.CancelFunc) {
			return context.WithDeadline(c, time.Unix(1<<40, 0))
		},
		Changed: func(f search.Facts) { changes++; store.Put("r", search.Fold(f)) },
		Failed: func(e *clidaemon.Unreadable) {
			v, _ := store.Get("r")
			v.Reason = core.StreamLost
			v.Detail = e.Detail
			store.Put("r", v)
		},
		Sleep: func(context.Context, time.Duration) error {
			sleeps++
			v, _ := store.Get("r")
			if len(v.Facts) != 1 || v.Reason != core.StreamLost || v.Detail == "" {
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
