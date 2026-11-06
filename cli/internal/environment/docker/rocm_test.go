package docker_test

import (
	"context"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/resource"
)

func withAMDGPUs(c *docker.Config) { c.Resources = append(c.Resources, amdOf("print", "gpu-ok")) }

// A task of the rocm backend runs on a placement that gives AMD GPUs: its container gets them, runs
// their check before the command, and the fingerprint records them.
func TestARocmTaskRunsWithTheAMDGPUsAfterTheirCheck(t *testing.T) {
	s := newScene(t, options{spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendROCm}, configure: withAMDGPUs})
	f := s.fingerprint()
	if f.Backend != core.BackendROCm || f.Facts["amdgpu_driver"] != "6.16.13" {
		t.Fatalf("fingerprint %v, want the rocm backend and the GPUs' facts", f)
	}
	keptContainer(s)
	// The container is kept to be read, so the run cannot vouch for its end.
	a, out := s.run("print", "x")
	if _, ok := a.(core.Unobserved); !ok {
		t.Fatalf("attempt %+v", a)
	}
	if strings.Contains(out, "gpu-ok") || strings.TrimSpace(out) != "x" {
		t.Errorf("output %q: the check's output is not the command's", out)
	}
	spec := onlyContainer(t, s.daemon)
	if spec.GPUs != (docker.GPUs{ROCm: true}) {
		t.Errorf("GPUs %+v, want the AMD GPUs", spec.GPUs)
	}
}

// A container whose image cannot use the GPUs fails before the command, with what the check said,
// and nothing is left running.
func TestAnImageThatCannotUseTheAMDGPUsFailsSayingWhy(t *testing.T) {
	said := "the container's ROCm runtime does not see the GPU: rocminfo lists no GPU agent (the image's ROCm 6.4.4, the node's amdgpu driver 6.16.13)"
	s := newScene(t, options{
		spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendROCm},
		configure: func(c *docker.Config) {
			c.Resources = append(c.Resources, amdOf(append(append([]string{"print"}, strings.Fields(said)...), ";", "exit", "1")...))
		},
	})
	a, out := s.run("mark", "ran")
	failure := envtest.NotStarted(t, a)
	if failure.Kind != core.FailureUnavailable || !strings.Contains(failure.Message, said) {
		t.Errorf("failure %+v, want what the check said", failure)
	}
	if s.world.Marked("ran") || out != "" {
		t.Errorf("the command ran (output %q)", out)
	}
	if left := s.daemon.Containers(); len(left) != 0 {
		t.Errorf("containers left: %+v", left)
	}
}

// In a task of two images, the AMD GPUs and their check are the server's: the command's container
// gets neither.
func TestTheAMDGPUsAreTheServersInATaskOfTwoImages(t *testing.T) {
	files := &candidateFiles{files: map[string]string{"serve/Dockerfile": "FROM busybox:1.36.1\n"}}
	timers := envtest.NewTimers()
	s := &candidateScene{scene: newScene(t, options{timers: timers, spec: docker.Spec{Dockerfile: []byte(simpleDockerfile), Backend: core.BackendROCm}, configure: func(c *docker.Config) {
		c.Candidates, c.Probe, c.ProbePause = files, probeProgram, probePause
		c.Resources = []resource.Resource{amdOf("mark", "checked")}
	}}), files: files, timers: timers}
	keptContainer(s.scene)
	ctx, cancel := context.WithTimeout(context.Background(), envtest.Backstop)
	defer cancel()
	wait := s.start(ctx, s.execution([]string{"ready", ";", "hold", "forever"}, "print", "done"))
	if a, out := wait(); !strings.Contains(out, "done") {
		t.Fatalf("attempt %+v, output %q", a, out)
	}
	if !s.world.Marked("checked") {
		t.Error("the server's container never ran the check")
	}
	for _, b := range s.daemon.Containers() {
		server := strings.HasSuffix(b.Name, "-candidate")
		if b.Spec.GPUs.ROCm != server {
			t.Errorf("%s: GPUs %+v", b.Name, b.Spec.GPUs)
		}
	}
}
