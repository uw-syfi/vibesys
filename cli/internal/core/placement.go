package core

import (
	"fmt"
	"regexp"
	"slices"
	"strings"
	"time"
)

// ResourceKind is something a placement can give a job's container, and a task can need. It is
// closed: a new kind is a contract change (ResourceKind in environment.proto).
type ResourceKind string

// The resource kinds. This is the one place in the domain that spells them; everything else takes
// a value of this type.
const (
	// ResourceGPU is GPUs passed into the container.
	ResourceGPU ResourceKind = "gpu"
	// ResourceKubernetes is access to a Kubernetes cluster.
	ResourceKubernetes ResourceKind = "kubernetes"
	// ResourceMounts is read-only directories of the engine's machine.
	ResourceMounts ResourceKind = "mounts"
	// ResourceROCm is AMD GPUs passed into the container, through the ROCm kernel driver.
	ResourceROCm ResourceKind = "rocm"
)

// ResourceKinds returns every resource kind, in the order the contract numbers them.
func ResourceKinds() []ResourceKind {
	return []ResourceKind{ResourceGPU, ResourceKubernetes, ResourceMounts, ResourceROCm}
}

// ParseResourceKind reads a resource kind by its name, and fails with FailureInvalidInput naming
// the kinds there are.
func ParseResourceKind(name string) (ResourceKind, error) {
	k := ResourceKind(name)
	if slices.Contains(ResourceKinds(), k) {
		return k, nil
	}
	return "", failf(FailureInvalidInput, "%q is not a resource kind (%s)", name, kindList(ResourceKinds()))
}

func kindList(kinds []ResourceKind) string {
	names := make([]string, len(kinds))
	for i, k := range kinds {
		names[i] = string(k)
	}
	return strings.Join(names, ", ")
}

// Claim is a share of something a job holds while it runs, which scheduling accounts for. The
// resource is an opaque id, such as "placement:local" or "kubernetes:<cluster>": two claims with
// one id are on the same thing. An exclusive claim shares its resource with no other claim.
type Claim struct {
	Resource  string
	Amount    int
	Exclusive bool
}

// placementName is the form of a placement's name: a TOML key and part of a resource id.
var placementName = regexp.MustCompile(`^[a-z0-9][a-z0-9_-]{0,62}$`)

// ValidPlacementName fails with FailureInvalidInput for a name a placement cannot have. The empty
// name is not one.
func ValidPlacementName(name string) error {
	if !placementName.MatchString(name) {
		return failf(FailureInvalidInput, "placement: %q is not a placement name (lower-case letters, digits, '_' and '-', starting with a letter or digit)", name)
	}
	return nil
}

// PlacementOffer is a placement of the operator config as choosing one sees it: its name, the
// kinds of resource it gives a container, and the kinds of job it runs.
type PlacementOffer struct {
	Name      string
	Resources []ResourceKind
	// Jobs are the kinds of job the placement runs, such as check and command on a partition whose
	// time limit is shorter than a measure takes. Empty means every kind.
	Jobs []JobKind
	// LongestRun is the longest a command may run there before what it runs in is ended under it,
	// such as a cluster's allocation capped at its partition's time limit, less the time its stop
	// takes. Zero is no cap. A lab there lasts no longer.
	LongestRun time.Duration
}

// LabTime is how long a lab that asks for asked lasts on the placement: no longer than its
// LongestRun.
func (p PlacementOffer) LabTime(asked time.Duration) time.Duration {
	if p.LongestRun > 0 && asked > p.LongestRun {
		return p.LongestRun
	}
	return asked
}

// Runs reports whether the placement runs a job of the kind.
func (p PlacementOffer) Runs(kind JobKind) bool {
	return len(p.Jobs) == 0 || slices.Contains(p.Jobs, kind)
}

// fits says why a job of the kind with the needs cannot run on the placement, or "" when it can.
func (p PlacementOffer) fits(kind JobKind, needs []ResourceKind) string {
	if !p.Runs(kind) {
		return fmt.Sprintf("placement %q runs no %s job (its job_kinds: %s)", p.Name, kind, jobKindList(p.Jobs))
	}
	if lack := p.Lacks(needs); lack != "" {
		return fmt.Sprintf("placement %q has no resource %q, which the task needs", p.Name, lack)
	}
	return ""
}

func jobKindList(kinds []JobKind) string {
	names := make([]string, len(kinds))
	for i, k := range kinds {
		names[i] = string(k)
	}
	return strings.Join(names, ", ")
}

// Lacks returns the first of needs the placement does not give, or "" when it gives them all.
func (p PlacementOffer) Lacks(needs []ResourceKind) ResourceKind {
	for _, n := range needs {
		if !slices.Contains(p.Resources, n) {
			return n
		}
	}
	return ""
}

// EligiblePlacements returns the placements a docker job of the kind may run on, by name: every
// offer that runs jobs of the kind and gives each needed resource, or, when the job is restricted to
// one placement, that one. Placements are interchangeable, so which of them a job runs on is the
// scheduler's choice when it starts (schedule.PlacementDemand), and every one of them is eligible
// as long as it runs the job's kind and gives what the task needs.
//
// It fails with FailureInvalidInput, naming the placements there are, for a restriction that is not
// a placement of the offers, that runs no job of the kind or that lacks a need, and when no offer
// fits. It never returns an empty list without an error.
func EligiblePlacements(offers []PlacementOffer, kind JobKind, needs []ResourceKind, restriction string) ([]string, error) {
	names := make([]string, len(offers))
	for i, o := range offers {
		names[i] = o.Name
	}
	slices.Sort(names)
	if restriction != "" {
		if err := ValidPlacementName(restriction); err != nil {
			return nil, err
		}
		i := slices.IndexFunc(offers, func(o PlacementOffer) bool { return o.Name == restriction })
		if i < 0 {
			return nil, failf(FailureInvalidInput, "placement %q is not in the operator config (its placements: %s)", restriction, strings.Join(names, ", "))
		}
		if why := offers[i].fits(kind, needs); why != "" {
			return nil, failf(FailureInvalidInput, "%s", why)
		}
		return []string{restriction}, nil
	}
	var fit, runs []string
	for _, o := range offers {
		if !o.Runs(kind) {
			continue
		}
		runs = append(runs, o.Name)
		if o.Lacks(needs) == "" {
			fit = append(fit, o.Name)
		}
	}
	if len(fit) == 0 {
		if len(runs) == 0 {
			return nil, failf(FailureInvalidInput, "no placement of the operator config runs a %s job (its job_kinds exclude it; its placements: %s)", kind, strings.Join(names, ", "))
		}
		what := "the task needs no resource"
		if len(needs) > 0 {
			what = "the task needs " + kindList(needs)
		}
		slices.Sort(runs)
		return nil, failf(FailureInvalidInput, "no placement of the operator config that runs a %s job gives what it needs (%s; those placements: %s)", kind, what, strings.Join(runs, ", "))
	}
	slices.Sort(fit)
	return slices.Compact(fit), nil
}
