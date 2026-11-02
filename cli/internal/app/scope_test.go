package app_test

import (
	"context"
	"errors"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// hungResolver is git on a mount that does not answer: it returns only when the call is given up.
type hungResolver struct{}

func (hungResolver) Resolve(ctx context.Context, _ string) (core.Repository, error) {
	<-ctx.Done()
	return "", ctx.Err()
}

// Resolving a caller's directory is bounded like a store call: a git that hangs is unavailable,
// which a caller tries again, and holds no call forever.
//
// The App is not started, as resolution needs no machine: so the hung resolver is the only call
// the bound can end, and no store call races the timer that fires it at once.
func TestAResolutionThatHangsIsUnavailable(t *testing.T) {
	w := newWorld(t)
	cfg := instant(app.Config{Clock: w.clock, Stores: w.stores, Environments: fakeenv.Environments{core.EnvironmentHost: w.env}, Supervisors: w.supervisors(), Repositories: hungResolver{}})
	cfg.CallTimeout = time.Second // the rig's timers fire this at once
	a := app.New(cfg)
	t.Cleanup(a.Close)
	var f *core.Failure
	if _, err := a.Resolve(ctxFor(t), app.Caller{Directory: "/repo", Task: "task"}); !errors.As(err, &f) || f.Kind != core.FailureUnavailable {
		t.Fatalf("a resolution that hangs: %v, want unavailable", err)
	}
}
