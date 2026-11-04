package schedule

import (
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// PlacementOf is the resource of a placement of the operator config: its slots, max_jobs of them.
func PlacementOf(name string) ResourceID { return ResourceID("placement:" + name) }

// PlacementDemand is the demand of a daemon whose config names placements. A docker job claims a
// slot of the placement it runs on and what that placement's resources claim; a job that runs on
// no placement, a host job, claims a slot of Host. A measure job, whose benchmark samples must not
// be disturbed, claims each of them whole, so it shares its placement and a cluster or GPUs its
// placement gives with no other job, while the jobs of other placements run beside it.
type PlacementDemand struct {
	// Placements are what a job of each placement claims besides the placement's slot: the claims
	// of the resources the placement gives, by placement name.
	Placements map[string][]Claim
	// Host is the resource a host job claims a slot of.
	Host ResourceID
}

// Claims implements Demand. The job's environment is decided: a job of EnvironmentAuto is given
// as the kind it runs in. A docker job with no placement, one queued before the daemon recorded
// placements, claims a slot of Host as such a job did then. One whose placement the config does not
// have claims that placement's slot, which no state grants, so it waits and says why.
func (d PlacementDemand) Claims(job core.Job) []Claim {
	measure := job.Spec.Settings != nil && job.Spec.Settings.Kind() == core.JobMeasure
	if job.Spec.Environment != core.EnvironmentDocker || job.Spec.Placement == "" {
		return []Claim{{Resource: d.Host, Amount: 1, Exclusive: measure}}
	}
	out := []Claim{{Resource: PlacementOf(job.Spec.Placement), Amount: 1, Exclusive: measure}}
	for _, c := range d.Placements[job.Spec.Placement] {
		c.Exclusive = c.Exclusive || measure
		c.Amount = max(c.Amount, 1)
		out = append(out, c)
	}
	return slices.Clip(out)
}
