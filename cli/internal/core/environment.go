package core

import "time"

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
