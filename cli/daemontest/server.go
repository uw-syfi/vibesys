// Package daemontest provides an in-process CLI daemon for cross-module tests.
package daemontest

import (
	"context"
	"testing"
	"time"

	pb "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/app/memstores"
	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/daemon"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
)

// Server starts the real daemon with an empty task in /repo and owns its cleanup.
func Server(t *testing.T) pb.DaemonServiceServer {
	t.Helper()
	env := fakeenv.New(core.EnvironmentHost, fakeenv.NewMemoryGates())
	envs := fakeenv.Environments{core.EnvironmentHost: env}
	survivors := supervisor.NewSurvivors()
	repos := gitrepo.NewFake()
	repos.AddRepo("/repo", nil)
	a := app.New(app.Config{Timers: clock.Immediate{}, Clock: clock.NewManual(time.Date(2026, 1, 1, 0, 0, 0, 0, time.UTC), time.Millisecond), Stores: memstores.New(app.Rules(5)), Environments: envs, Supervisors: supervisor.NewInProcess(envs, &fakeenv.Checkouts{}, survivors), Repositories: repos, Epoch: "E1"})
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if err := a.Start(ctx); err != nil {
		survivors.Close()
		t.Fatal(err)
	}
	srv := daemon.NewServer(a, "test")
	t.Cleanup(func() { srv.Shutdown(); a.Close(); survivors.Close() })
	return srv
}
