package core

import (
	"regexp"
	"slices"
	"strings"
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
)

// ResourceKinds returns every resource kind, in the order the contract numbers them.
func ResourceKinds() []ResourceKind {
	return []ResourceKind{ResourceGPU, ResourceKubernetes, ResourceMounts}
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

// PlacementOffer is a placement of the operator config as choosing one sees it: its name and the
// kinds of resource it gives a container.
type PlacementOffer struct {
	Name      string
	Resources []ResourceKind
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

// EligiblePlacements returns the placements a docker job may run on, by name: every offer that
// gives each needed resource, or, when the job is restricted to one placement, that one. Placements
// are interchangeable, so which of them a job runs on is the scheduler's choice when it starts
// (schedule.PlacementDemand), and every one of them is eligible as long as it gives what the task
// needs.
//
// It fails with FailureInvalidInput, naming the placements there are, for a restriction that is not
// a placement of the offers or that lacks a need, and when no offer gives every need. It never
// returns an empty list without an error.
func EligiblePlacements(offers []PlacementOffer, needs []ResourceKind, restriction string) ([]string, error) {
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
		if lack := offers[i].Lacks(needs); lack != "" {
			return nil, failf(FailureInvalidInput, "placement %q has no resource %q, which the task needs", restriction, lack)
		}
		return []string{restriction}, nil
	}
	var fit []string
	for _, o := range offers {
		if o.Lacks(needs) == "" {
			fit = append(fit, o.Name)
		}
	}
	if len(fit) == 0 {
		what := "the task needs no resource"
		if len(needs) > 0 {
			what = "the task needs " + kindList(needs)
		}
		return nil, failf(FailureInvalidInput, "no placement of the operator config gives what it needs (%s; its placements: %s)", what, strings.Join(names, ", "))
	}
	slices.Sort(fit)
	return slices.Compact(fit), nil
}
