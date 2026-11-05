package docker

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"slices"
	"strconv"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// The labels every image and container this package creates carries. A sweep removes only
// containers that carry them, so a container of someone else is never touched.
const (
	// LabelOwner marks what this tool made. Its value is Owner.
	LabelOwner = "io.vibesys.cli.owner"
	// LabelScope names the daemon's scope: the containers of one scope are one run at a time.
	LabelScope = "io.vibesys.cli.scope"
	// LabelRun names the run a container belongs to.
	LabelRun = "io.vibesys.cli.run"

	// Owner is the value of LabelOwner.
	Owner = "vibesys-cli"
)

const (
	containerPrefix = Owner + "-"
	imageRepository = "vibesys-cli-task"
)

var (
	scopePattern = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}$`)
	runIDPattern = regexp.MustCompile(`^[a-f0-9]{8,64}$`)
)

// scopeLabels are the labels a sweep looks for: everything this tool made in the scope.
func scopeLabels(scope string) Labels {
	return Labels{LabelOwner: Owner, LabelScope: scope}
}

// runLabels are the labels of one run's container.
func runLabels(scope, runID string) Labels {
	l := scopeLabels(scope)
	l[LabelRun] = runID
	return l
}

// containerName is the unique name of the container of the run with the id.
func containerName(runID string) string { return containerPrefix + runID }

// imageTag names the image a build context builds, by a digest of the context: the same task
// files are the same tag, so an unchanged task finds its image already built.
func imageTag(context []byte) string {
	sum := sha256.Sum256(context)
	return imageRepository + ":" + hex.EncodeToString(sum[:16])
}

// Where a container finds what the environment gives it besides the checkout, and the variables
// that name them.
const (
	// CacheDir is the cache directory of the task's image, kept across runs and commits.
	CacheDir = "/vibesys/cache"
	// ArtifactsDir is the run's artifacts directory, empty when the run starts and kept with the
	// job after it.
	ArtifactsDir = "/vibesys/artifacts"

	EnvCacheDir     = "VIBESYS_CACHE_DIR"
	EnvArtifactsDir = "VIBESYS_ARTIFACTS_DIR"
)

// cacheKey names the cache directory of a task's image: the task and the image, so the cache is
// shared by every commit that runs in the image and a new image starts with an empty one.
func cacheKey(taskName, imageID string) string {
	return taskName + "-" + strings.TrimPrefix(imageID, "sha256:")[:16]
}

// leftovers returns the names to remove before a run starts: the containers found in the scope
// that no live run of this process owns, in order and once each. A container found twice, or one
// that a live run owns, is not in the result.
func leftovers(found []string, owned map[string]struct{}) []string {
	var out []string
	for _, name := range found {
		if _, mine := owned[name]; mine || slices.Contains(out, name) {
			continue
		}
		out = append(out, name)
	}
	slices.Sort(out)
	return out
}

// backendOf is the backend a spec targets.
func backendOf(spec Spec) core.BackendKind {
	if spec.Backend == "" {
		return core.BackendCPU
	}
	return spec.Backend
}

// refusal says why a container of this build on the placement cannot run the task, or returns
// nil. It is what "refuse clearly" means: the task asks for something the container cannot give,
// and the error names it and what to do instead, before anything is built or started.
func refusal(spec Spec, placement core.PlacementOffer) error {
	unavailable := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "docker: " + fmt.Sprintf(format, args...)}
	}
	if lack := placement.Lacks(spec.Needs); lack != "" {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"placement %q has no resource %q, which the task needs; give it one in the operator config, or let the job run on any placement that has one by not restricting it with --placement", placement.Name, lack)}
	}
	switch backend := backendOf(spec); {
	case backend == core.BackendCUDA && placement.Lacks([]core.ResourceKind{core.ResourceGPU}) != "":
		return unavailable("the task targets the cuda backend, and the placement %q passes no GPU into a container; give it a gpu resource in the operator config, or run it on the host with --env host", placement.Name)
	case backend == core.BackendROCm && placement.Lacks([]core.ResourceKind{core.ResourceROCm}) != "":
		return unavailable("the task targets the rocm backend, and the placement %q passes no AMD GPU into a container; give it a rocm resource in the operator config, or run it on the host with --env host", placement.Name)
	case backend != core.BackendCPU && backend != core.BackendCUDA && backend != core.BackendROCm:
		return unavailable("the task targets the %q backend, and a container of this build gets GPUs only for the cuda and rocm backends; run it on the host with --env host", backend)
	case spec.NestedDocker:
		return unavailable("the task starts containers of its own (docker_in_docker = true), and a container of this build has no container runtime in it; run it on the host with --env host")
	}
	return nil
}

// facts are the fingerprint facts that say where the container ran: the machine, the platform of
// its image, and the placement (core.FactPlacement); the resources add their own. They are kept
// with each result to tell where a sample came from, and take no part in which results are
// compared or reused: placements are interchangeable, so that is decided by the image digest and
// the backend alone (core.Fingerprint.Equal).
func facts(m core.MachineFacts, img Image, placement string) map[string]string {
	return map[string]string{
		"os":               m.OS,
		"arch":             m.Arch,
		"cpu_model":        m.CPUModel,
		"logical_cpus":     strconv.FormatUint(uint64(m.LogicalCPUs), 10),
		"image_platform":   img.Platform,
		core.FactPlacement: placement,
	}
}

// checkRunID refuses an id that cannot be part of a container name or a label.
func checkRunID(id string) error {
	if !runIDPattern.MatchString(id) {
		return &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: %q is not a run id (8 to 64 lower-case hexadecimal digits)", id)}
	}
	return nil
}

// ScopeFor returns the scope of whatever the key names, which a daemon takes to be what is unique
// to it, such as the directory its runtime files are in. Two keys name two scopes, and the same key
// names the same scope in every process, so a daemon that starts again sweeps what the one before
// it left, and two daemons of one user never sweep each other's containers.
func ScopeFor(key string) string {
	sum := sha256.Sum256([]byte(key))
	return "d" + hex.EncodeToString(sum[:8])
}

// checkScope refuses a scope that cannot be a label value or part of one.
func checkScope(scope string) error {
	if !scopePattern.MatchString(scope) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: %q is not a scope name (letters, digits, '_', '.' and '-', at most 63 characters)", scope)}
	}
	return nil
}
