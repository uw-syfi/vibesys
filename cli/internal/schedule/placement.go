package schedule

import (
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// PlacementOf is the resource of a placement of the operator config: its slots, max_jobs of them.
func PlacementOf(name string) ResourceID { return ResourceID("placement:" + name) }

// Offered is a placement of the operator config as a demand sees it: the kinds of resource it gives
// a container, and what a job on it claims of them besides the placement's slot.
type Offered struct {
	Resources []core.ResourceKind
	Claims    []Claim
}

// PlacementDemand is the demand of a daemon whose config names placements. Placements are
// interchangeable: a queued docker job may run on every placement that gives each resource its
// task needs (core.EligiblePlacements), or on the one its submission restricted it to, and Step
// chooses among them when it starts. On a placement it claims a slot of it and what that
// placement's resources claim. A job that runs on no placement, a host job, claims a slot of Host.
// A measure job, whose benchmark samples must not be disturbed, claims each of them whole, so it
// shares its placement and a cluster or GPUs its placement gives with no other job, while the jobs
// of other placements run beside it.
type PlacementDemand struct {
	// Placements are the placements of the operator config, by name.
	Placements map[string]Offered
	// Host is the resource a host job claims a slot of.
	Host ResourceID
}

// Options implements Demand. The job's environment is decided: a job of EnvironmentAuto is given
// as the kind it runs in.
//
// A job that started holds the placement it started on, which its pinned environment names
// (core.Fingerprint.Placement), so its only option is that one. A job of a store of an earlier
// version, whose environment names none, held the placement its submission was given, and one from
// before placements were recorded claims a slot of Host as such a job did then.
//
// A queued docker job that no placement of the config fits, or restricted to one the config does not
// have or that lacks a need, has no option, so Step refuses it.
func (d PlacementDemand) Options(job core.Job) []Option {
	measure := job.Spec.Settings != nil && job.Spec.Settings.Kind() == core.JobMeasure
	if job.Spec.Environment != core.EnvironmentDocker {
		return Only(Claim{Resource: d.Host, Amount: 1, Exclusive: measure})
	}
	if job.Environment != nil {
		placement := job.Environment.Placement()
		if placement == "" {
			placement = job.Spec.Placement
		}
		if placement == "" {
			return Only(Claim{Resource: d.Host, Amount: 1, Exclusive: measure})
		}
		return []Option{d.on(placement, measure)}
	}
	offers := make([]core.PlacementOffer, 0, len(d.Placements))
	for name, o := range d.Placements {
		offers = append(offers, core.PlacementOffer{Name: name, Resources: o.Resources})
	}
	names, err := core.EligiblePlacements(offers, job.Needs, job.Spec.Placement)
	if err != nil {
		return nil
	}
	out := make([]Option, len(names))
	for i, name := range names {
		out[i] = d.on(name, measure)
	}
	return out
}

// on is the option of running on the placement: a slot of it, and what its resources claim.
func (d PlacementDemand) on(name string, measure bool) Option {
	claims := []Claim{{Resource: PlacementOf(name), Amount: 1, Exclusive: measure}}
	for _, c := range d.Placements[name].Claims {
		c.Exclusive = c.Exclusive || measure
		c.Amount = max(c.Amount, 1)
		claims = append(claims, c)
	}
	return Option{Placement: name, Claims: slices.Clip(claims)}
}
