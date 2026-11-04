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

// ChoosePlacement decides which placement a docker job runs on. A requested placement must be one
// of the offers and give every needed resource. With none requested, the one placement that gives
// every needed resource is chosen; when none does, or more than one does, the job is refused,
// naming them, since a guess would put a job's numbers on a machine the caller did not mean. It
// fails with FailureInvalidInput and never returns the empty name.
func ChoosePlacement(offers []PlacementOffer, needs []ResourceKind, requested string) (string, error) {
	names := make([]string, len(offers))
	for i, o := range offers {
		names[i] = o.Name
	}
	slices.Sort(names)
	if requested != "" {
		if err := ValidPlacementName(requested); err != nil {
			return "", err
		}
		i := slices.IndexFunc(offers, func(o PlacementOffer) bool { return o.Name == requested })
		if i < 0 {
			return "", failf(FailureInvalidInput, "placement %q is not in the operator config (its placements: %s)", requested, strings.Join(names, ", "))
		}
		if lack := offers[i].Lacks(needs); lack != "" {
			return "", failf(FailureInvalidInput, "placement %q has no resource %q, which the task needs", requested, lack)
		}
		return requested, nil
	}
	var fit []string
	for _, o := range offers {
		if o.Lacks(needs) == "" {
			fit = append(fit, o.Name)
		}
	}
	slices.Sort(fit)
	what := "the task needs no resource"
	if len(needs) > 0 {
		what = "the task needs " + kindList(needs)
	}
	switch len(fit) {
	case 1:
		return fit[0], nil
	case 0:
		return "", failf(FailureInvalidInput, "no placement of the operator config gives what it needs (%s; its placements: %s)", what, strings.Join(names, ", "))
	}
	return "", failf(FailureInvalidInput, "the placements %s each give what it needs (%s); choose one with --placement or VIBESYS_PLACEMENT", strings.Join(fit, ", "), what)
}
