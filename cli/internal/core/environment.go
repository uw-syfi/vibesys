package core

import (
	"maps"
	"regexp"
	"slices"
	"strings"
	"time"
)

// EnvironmentKind is where a job's commands run.
type EnvironmentKind string

// The environment kinds. The zero value, EnvironmentAuto, asks the task to choose.
const (
	EnvironmentAuto   EnvironmentKind = ""
	EnvironmentHost   EnvironmentKind = "host"
	EnvironmentDocker EnvironmentKind = "docker"
)

// BackendKind is the compute stack a task targets. It is closed: a new stack is a contract change.
type BackendKind string

// The backend kinds.
const (
	BackendCPU      BackendKind = "cpu"
	BackendCUDA     BackendKind = "cuda"
	BackendROCm     BackendKind = "rocm"
	BackendMetal    BackendKind = "metal"
	BackendTrainium BackendKind = "trainium"
)

// Known reports whether the kind is one of the five backends.
func (b BackendKind) Known() bool {
	switch b {
	case BackendCPU, BackendCUDA, BackendROCm, BackendMetal, BackendTrainium:
		return true
	}
	return false
}

// Fingerprint identifies the environment a result was taken in. Two results are comparable, and a
// check pass reusable, only when their fingerprints are equal.
type Fingerprint struct {
	Kind    EnvironmentKind
	Backend BackendKind
	// ImageDigest is the exact image digest for the docker environment and empty for host.
	ImageDigest string
	// Facts are what the environment pinned, by name: operating system, architecture, toolchains.
	Facts map[string]string
}

// imageDigest is an exact image digest: a tag names an image that can change, a digest cannot.
var imageDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// Equal reports whether two fingerprints name the same environment. Facts are compared as a
// whole, and a nil map equals an empty one.
func (f Fingerprint) Equal(other Fingerprint) bool {
	return f.Kind == other.Kind && f.Backend == other.Backend && f.ImageDigest == other.ImageDigest &&
		maps.Equal(f.Facts, other.Facts)
}

// String names the environment for display: its kind, backend, image digest and facts.
func (f Fingerprint) String() string {
	var b strings.Builder
	b.WriteString(string(f.Kind))
	b.WriteString("/")
	b.WriteString(string(f.Backend))
	if f.ImageDigest != "" {
		b.WriteString("@")
		b.WriteString(f.ImageDigest)
	}
	for _, name := range slices.Sorted(maps.Keys(f.Facts)) {
		b.WriteString(" " + name + "=" + f.Facts[name])
	}
	return b.String()
}

// validatePinned checks that the fingerprint is of an environment a job ran in: a host or docker
// kind, a known backend, and for docker the exact image digest.
func (f Fingerprint) validatePinned() error {
	switch f.Kind {
	case EnvironmentHost:
		if f.ImageDigest != "" {
			return failf(FailureInvalidInput, "environment: a host environment has no image digest, got %q", f.ImageDigest)
		}
	case EnvironmentDocker:
		if !imageDigest.MatchString(f.ImageDigest) {
			return failf(FailureInvalidInput, "environment: a docker environment pins an image digest (sha256:<64 hex digits>), got %q", f.ImageDigest)
		}
	default:
		return failf(FailureInvalidInput, "environment: kind %q is not a pinned host or docker environment", f.Kind)
	}
	if !f.Backend.Known() {
		return failf(FailureInvalidInput, "environment: backend %q is not a known backend", f.Backend)
	}
	return nil
}

// TaskSetup is what a task declares that decides where its jobs run.
type TaskSetup struct {
	Backend BackendKind
	// HasDockerfile is set when the task ships its own Dockerfile.
	HasDockerfile bool
	// Image names the image the task runs in, or is empty.
	Image string
}

// ChooseEnvironment decides where a task's jobs run. A task with a Dockerfile or a named image
// runs in docker and any other task on the host. The caller's request overrides that, except on
// the Metal backend, which needs the host. It fails with FailureInvalidInput rather than fall
// back to another kind when the task cannot run where it asks: Metal with a Dockerfile, a named
// image or a request for docker, or a backend that is not known or a request that is not a kind.
// It never returns EnvironmentAuto.
func ChooseEnvironment(task TaskSetup, requested EnvironmentKind) (EnvironmentKind, error) {
	if !task.Backend.Known() {
		return "", failf(FailureInvalidInput, "backend: %q is not a known backend", task.Backend)
	}
	switch requested {
	case EnvironmentAuto, EnvironmentHost, EnvironmentDocker:
	default:
		return "", failf(FailureInvalidInput, "environment: %q is neither %q nor %q", requested, EnvironmentHost, EnvironmentDocker)
	}
	if task.Backend == BackendMetal {
		switch {
		case task.HasDockerfile:
			return "", failf(FailureInvalidInput, "environment: the metal backend needs the host, and the task has a Dockerfile")
		case task.Image != "":
			return "", failf(FailureInvalidInput, "environment: the metal backend needs the host, and the task names the image %q", task.Image)
		case requested == EnvironmentDocker:
			return "", failf(FailureInvalidInput, "environment: the metal backend needs the host, so it cannot run in docker")
		}
		return EnvironmentHost, nil
	}
	switch {
	case requested != EnvironmentAuto:
		return requested, nil
	case task.HasDockerfile || task.Image != "":
		return EnvironmentDocker, nil
	}
	return EnvironmentHost, nil
}

// MachineFacts describes the machine a result was taken on.
type MachineFacts struct {
	HostID      string
	OS          string
	Arch        string
	CPUModel    string
	LogicalCPUs uint32
	MemoryBytes uint64
}

// MachineLoad is the machine's load when it was sampled. It is reported, never acted on.
type MachineLoad struct {
	SampledAt            time.Time
	LoadAverage1m        float64
	CPUBusyFraction      float64
	MemoryAvailableBytes uint64
}
