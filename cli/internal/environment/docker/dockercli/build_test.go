//go:build unix

package dockercli_test

import (
	"context"
	"io"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker/dockercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// failingDocker stands for a docker whose every build fails, as a Dockerfile that does not build.
const failingDocker = `#!/bin/sh
cat >/dev/null
echo "ERROR: failed to solve: process did not complete successfully" >&2
exit 1
`

// Regression: a failed build said "building the task's image failed" whichever image it was
// building, so a task with a [candidate] whose candidate's image failed blamed the task's. A failed
// build names the image it was building, by its role and its tag.
func TestAFailedBuildNamesTheImageItWasBuilding(t *testing.T) {
	program := filepath.Join(t.TempDir(), "docker")
	if err := os.WriteFile(program, []byte(failingDocker), 0o755); err != nil {
		t.Fatal(err)
	}
	engine, err := dockercli.New(dockercli.Config{Program: program, Timers: clock.System{}})
	if err != nil {
		t.Fatal(err)
	}
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	const tag = "vibesys-test:0123abcd"
	for role, want := range map[docker.ImageRole]string{
		docker.TaskImage:      "building the task's image (" + tag + ") failed",
		docker.CandidateImage: "building the candidate's image (" + tag + ") failed",
		"":                    "building the image " + tag + " failed",
	} {
		err := engine.Build(ctx, docker.BuildSpec{Tag: tag, Context: []byte("a build context"), Role: role}, io.Discard)
		if err == nil || !strings.Contains(err.Error(), want) || !strings.Contains(err.Error(), "failed to solve") {
			t.Errorf("role %q: %v, want it to say %q and the build's output", role, err, want)
		}
	}
}
