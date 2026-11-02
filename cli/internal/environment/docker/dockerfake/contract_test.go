package dockerfake_test

import (
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockerfake"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockertest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv/fakeworld"
)

// The fake engine passes the same contract the real one does, which is what lets the docker
// environment's simulations stand in for a real daemon.
func TestFakeEngineMeetsTheEngineContract(t *testing.T) {
	dockertest.Run(t, dockertest.Rig{
		World: func(*testing.T) envtest.World { return fakeworld.New() },
		Dir:   func(t *testing.T, _ envtest.World) string { return t.TempDir() },
		Engine: func(_ *testing.T, w envtest.World) docker.Engine {
			return dockerfake.NewDaemon(w.(fakeworld.World)).Connect()
		},
		Dockerfile: []byte("FROM scratch\n"),
	})
}
