package docker

import (
	"crypto/sha256"
	"encoding/hex"
	"fmt"
	"regexp"
	"slices"
	"strconv"

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

// imageTag names the image a Dockerfile builds, by a digest of the Dockerfile's text: the same
// text is the same tag, so an unchanged task finds its image already built.
func imageTag(dockerfile []byte) string {
	sum := sha256.Sum256(dockerfile)
	return imageRepository + ":" + hex.EncodeToString(sum[:16])
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

// refusal says why a container of this build cannot run the task, or returns nil. It is what
// "refuse clearly" means: the task asks for something the image cannot give, and the error names
// it and what to do instead, before anything is built or started.
func refusal(spec Spec, backend core.BackendKind) error {
	unavailable := func(format string, args ...any) error {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "docker: " + fmt.Sprintf(format, args...)}
	}
	switch {
	case backend != core.BackendCPU:
		return unavailable("the task targets the %q backend, and this build passes no accelerator into a container, only cpu; run it on the host with --env host", backend)
	case spec.NestedDocker:
		return unavailable("the task starts containers of its own (docker_in_docker = true), and a container of this build has no container runtime in it; run it on the host with --env host")
	}
	return CheckDockerfile(spec.Dockerfile)
}

// facts are the fingerprint facts that identify the machine the container runs on and the
// platform of its image. Results taken in the same image on machines with the same operating
// system, architecture, CPU model and CPU count are comparable; the machine's name and memory
// do not count.
func facts(m core.MachineFacts, img Image) map[string]string {
	return map[string]string{
		"os":             m.OS,
		"arch":           m.Arch,
		"cpu_model":      m.CPUModel,
		"logical_cpus":   strconv.FormatUint(uint64(m.LogicalCPUs), 10),
		"image_platform": img.Platform,
	}
}

// checkRunID refuses an id that cannot be part of a container name or a label.
func checkRunID(id string) error {
	if !runIDPattern.MatchString(id) {
		return &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("docker: %q is not a run id (8 to 64 lower-case hexadecimal digits)", id)}
	}
	return nil
}

// checkScope refuses a scope that cannot be a label value or part of one.
func checkScope(scope string) error {
	if !scopePattern.MatchString(scope) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("docker: %q is not a scope name (letters, digits, '_', '.' and '-', at most 63 characters)", scope)}
	}
	return nil
}
