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
// check pass reusable, only when their fingerprints are equal (Equal), and a fingerprint takes part
// in the cache key by what Equal compares.
//
// What identifies an environment depends on its kind. A docker environment is its image's content
// digest and backend: the image pins every program the job runs, so where its container ran (the
// machine, the placement, what the placement's resources gave it) is recorded in Facts for
// diagnosis and compared by nothing, which is what lets a job run on any placement that gives its
// task what it needs. The host has no image, so its Facts (the operating system, the architecture,
// the toolchains) are what identify it.
type Fingerprint struct {
	Kind    EnvironmentKind
	Backend BackendKind
	// ImageDigest is, for the docker environment, the digest of the content of the task's image:
	// of its config, platform and layers (docker.Engine.Content), and not its ID, which under
	// Docker's containerd image store differs from one build of the same content to the next. It
	// is empty for host. Results recorded before the content digest was pinned carry the image's
	// ID here instead; the daemon resolves such an ID to its content while the image is still on
	// its placement's engine, and one it cannot resolve equals no content digest.
	ImageDigest string
	// Facts are what the environment pinned, by name: operating system, architecture, toolchains.
	// A docker environment's facts also name the placement it ran on (FactPlacement) and what
	// identifies each resource its container was given.
	Facts map[string]string
}

// FactPlacement is the fact of a docker fingerprint that names the placement of the operator config
// its container ran on.
const FactPlacement = "placement"

// Placement is the placement the environment ran on, or "" for one that names none, such as the
// host.
func (f Fingerprint) Placement() string { return f.Facts[FactPlacement] }

// imageDigest is an exact image digest: a tag names an image that can change, a digest cannot.
var imageDigest = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)

// identity is the part of the fingerprint that identifies the environment: all of it for the host,
// and for docker everything but its facts.
func (f Fingerprint) identity() Fingerprint {
	if f.Kind == EnvironmentDocker {
		f.Facts = nil
	}
	return f
}

// Equal reports whether two fingerprints name the same environment: the same kind, backend and
// image digest, and for the host the same facts, compared as a whole, where a nil map equals an
// empty one. A docker environment's facts say where it ran, and do not count.
func (f Fingerprint) Equal(other Fingerprint) bool {
	a, b := f.identity(), other.identity()
	return a.Kind == b.Kind && a.Backend == b.Backend && a.ImageDigest == b.ImageDigest &&
		maps.Equal(a.Facts, b.Facts)
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
	// TwoImages says the task's candidate has an image of its own (task schema 0.2), so its jobs
	// run in docker.
	TwoImages bool
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
	case task.TwoImages && requested == EnvironmentHost:
		return "", failf(FailureInvalidInput, "environment: the task runs its candidate in an image of the candidate's own and its commands in the task's image, so it cannot run on the host")
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
