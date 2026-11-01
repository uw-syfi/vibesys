package core_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var allBackends = []core.BackendKind{core.BackendCPU, core.BackendCUDA, core.BackendROCm, core.BackendMetal, core.BackendTrainium}

// The rule over its whole input space: five backends, a Dockerfile or not, an image or not, and
// three requests. 60 cases, each against the table in the scope: a Dockerfile or an image means
// docker, otherwise host; a request overrides that; Metal needs the host, and anything that asks
// for docker there is refused, not quietly run on the host.
func TestChooseEnvironmentOverEveryInput(t *testing.T) {
	for _, backend := range allBackends {
		for _, dockerfile := range []bool{false, true} {
			for _, image := range []string{"", "ubuntu@sha256:abc"} {
				for _, requested := range []core.EnvironmentKind{core.EnvironmentAuto, core.EnvironmentHost, core.EnvironmentDocker} {
					task := core.TaskSetup{Backend: backend, HasDockerfile: dockerfile, Image: image}
					got, err := core.ChooseEnvironment(task, requested)
					wantsDocker := dockerfile || image != ""
					switch {
					case backend == core.BackendMetal && (wantsDocker || requested == core.EnvironmentDocker):
						if failureKind(err) != core.FailureInvalidInput || !strings.Contains(err.Error(), "metal") || got != "" {
							t.Errorf("%+v requested %q: got %q, %v; want a refusal that names metal", task, requested, got, err)
						}
					case backend == core.BackendMetal:
						if err != nil || got != core.EnvironmentHost {
							t.Errorf("%+v requested %q: got %q, %v; want host", task, requested, got, err)
						}
					case requested != core.EnvironmentAuto:
						if err != nil || got != requested {
							t.Errorf("%+v requested %q: got %q, %v; want the request", task, requested, got, err)
						}
					case wantsDocker:
						if err != nil || got != core.EnvironmentDocker {
							t.Errorf("%+v: got %q, %v; want docker", task, got, err)
						}
					default:
						if err != nil || got != core.EnvironmentHost {
							t.Errorf("%+v: got %q, %v; want host", task, got, err)
						}
					}
				}
			}
		}
	}
}

func TestChooseEnvironmentRefusesWhatItDoesNotKnow(t *testing.T) {
	for _, c := range []struct {
		name      string
		task      core.TaskSetup
		requested core.EnvironmentKind
		names     string
	}{
		{"no backend", core.TaskSetup{}, core.EnvironmentAuto, "backend"},
		{"unknown backend", core.TaskSetup{Backend: "tpu"}, core.EnvironmentAuto, "backend"},
		{"unknown request", core.TaskSetup{Backend: core.BackendCPU}, "podman", "podman"},
	} {
		got, err := core.ChooseEnvironment(c.task, c.requested)
		if failureKind(err) != core.FailureInvalidInput || !strings.Contains(err.Error(), c.names) || got != "" {
			t.Errorf("%s: got %q, %v; want an invalid_input failure naming %q", c.name, got, err, c.names)
		}
	}
}

func TestFingerprintEquality(t *testing.T) {
	a := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
	same := core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux"}}
	if !a.Equal(same) || !same.Equal(a) {
		t.Error("two fingerprints of the same facts are unequal")
	}
	empty, none := core.Fingerprint{Kind: core.EnvironmentHost}, core.Fingerprint{Kind: core.EnvironmentHost, Facts: map[string]string{}}
	if !empty.Equal(none) {
		t.Error("nil facts and empty facts are unequal")
	}
	differs := []core.Fingerprint{
		{Kind: core.EnvironmentDocker, Backend: core.BackendCPU, Facts: a.Facts},
		{Kind: core.EnvironmentHost, Backend: core.BackendCUDA, Facts: a.Facts},
		{Kind: core.EnvironmentHost, Backend: core.BackendCPU, ImageDigest: "sha256:x", Facts: a.Facts},
		{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "darwin"}},
		{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{"os": "linux", "arch": "arm64"}},
		{Kind: core.EnvironmentHost, Backend: core.BackendCPU},
	}
	for _, other := range differs {
		if a.Equal(other) || other.Equal(a) {
			t.Errorf("%v equals %v", a, other)
		}
	}
}
