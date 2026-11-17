package schedule

import (
	"slices"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// PlacementOf is the resource of a placement of the operator config: its slots, max_jobs of them.
func PlacementOf(name string) ResourceID { return ResourceID("placement:" + name) }

// Pool is the GPUs of a machine that its jobs are given by number: a resource of Size devices (its
// Resource.Devices is set) that a job claims some of, or all of.
type Pool struct {
	// Resource is the pool's resource, which names the machine whose GPUs they are.
	Resource ResourceID
	// Size is how many devices it has: its capacity.
	Size int
	// Kind is the kind of resource a docker job's task needs of a placement to use the pool.
	Kind core.ResourceKind
}

// Offered is a placement of the operator config as a demand sees it: the kinds of resource it gives
// a container, and what a job on it claims of them besides the placement's slot.
type Offered struct {
	Resources []core.ResourceKind
	Claims    []Claim
	// Pool, when set, is the pool of GPUs the placement's jobs are given by number, which a job
	// claims as PoolClaims says, besides Claims.
	Pool *Pool
	// Isolated says the placement already keeps a measure apart from its other jobs: each job runs
	// in allocations of its own, and a measured container's holds a whole node that no other job
	// shares. A measure of it then claims a slot of it and what its resources claim as any job
	// does, so that up to max_jobs measures, and other jobs, run on it at once, each on its own
	// node. Otherwise a measure holds the placement whole.
	Isolated bool
}

// PlacementDemand is the demand of a daemon whose config names placements. Placements are
// interchangeable: a queued docker job may run on every placement that gives each resource its
// task needs (core.EligiblePlacements), or on the one its submission restricted it to, and Step
// chooses among them when it starts. On a placement it claims a slot of it and what that
// placement's resources claim. A job that runs on no placement, a host job, claims a slot of Host.
// A measure job, whose benchmark samples must not be disturbed, claims each of them whole, so it
// shares its placement and a cluster or GPUs its placement gives with no other job, while the jobs
// of other placements run beside it; on a placement that is Isolated, where its node is already
// its own, it claims them as any job does.
type PlacementDemand struct {
	// Placements are the placements of the operator config, by name.
	Placements map[string]Offered
	// Host is the resource a host job claims a slot of.
	Host ResourceID
	// HostPool, when set, is the pool of GPUs of this machine, the one a local placement declares,
	// which a host job claims as PoolClaims says, so that host and docker jobs on this machine
	// never share a GPU.
	HostPool *Pool
}

// PoolClaims is what a job claims of a pool while it holds the machine, or nothing for a job that
// uses none of its GPUs:
//
//   - a job that holds a grant of the pool (it started) claims the devices of the grant, and
//     alone when the grant is the whole pool;
//   - one that asked for GPUs (--gpus N) claims N;
//   - a measure takes the whole pool alone, as it takes its placement: its benchmark must not be
//     disturbed, whether or not its task needs GPUs;
//   - a job whose task needs the pool's kind and that asked for no count takes the whole pool,
//     which is what it always had. A submission that asks no count of a placement with a pool is
//     refused before it is queued, so this is what a job of an older store does;
//   - a job that started before grants were recorded holds what such a job holds, alone: it may
//     use every device, and nobody can say which.
func PoolClaims(p *Pool, job core.Job, measure bool) []Claim {
	if p == nil {
		return nil
	}
	whole := []Claim{{Resource: p.Resource, Amount: p.Size, Exclusive: true}}
	started := job.Environment != nil
	needsPool := slices.Contains(job.Needs, p.Kind)
	switch g := job.GPUs; {
	case g != nil && g.Pool == core.PoolID(p.Resource):
		return []Claim{{Resource: p.Resource, Amount: max(g.Devices.Len(), 1), Exclusive: g.Whole}}
	case started && (measure || needsPool):
		return whole
	case started:
		return nil
	case job.Spec.GPUs.IsSet():
		return []Claim{{Resource: p.Resource, Amount: job.Spec.GPUs.Int()}}
	case measure, needsPool:
		return whole
	}
	return nil
}

// Options implements Demand. A job of EnvironmentAuto, which only an older daemon queued and which
// a launch refuses (App.prepare), claims the host as a host job does.
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
		return d.onHost(job, measure)
	}
	if job.Environment != nil {
		placement := job.Environment.Placement()
		if placement == "" {
			placement = job.Spec.Placement
		}
		if placement == "" {
			return d.onHost(job, measure)
		}
		return []Option{d.on(placement, job, measure)}
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
		out[i] = d.on(name, job, measure)
	}
	return out
}

// onHost is the one option of a host job: a slot of Host, and what it claims of the machine's pool.
func (d PlacementDemand) onHost(job core.Job, measure bool) []Option {
	claims := []Claim{{Resource: d.Host, Amount: 1, Exclusive: measure}}
	return []Option{{Claims: append(claims, PoolClaims(d.HostPool, job, measure)...)}}
}

// on is the option of running on the placement: a slot of it, what its resources claim, and what
// the job claims of its pool.
func (d PlacementDemand) on(name string, job core.Job, measure bool) Option {
	offered := d.Placements[name]
	pool := PoolClaims(offered.Pool, job, measure)
	if offered.Isolated {
		measure = false
	}
	claims := []Claim{{Resource: PlacementOf(name), Amount: 1, Exclusive: measure}}
	for _, c := range offered.Claims {
		c.Exclusive = c.Exclusive || measure
		c.Amount = max(c.Amount, 1)
		claims = append(claims, c)
	}
	return Option{Placement: name, Claims: slices.Clip(append(claims, pool...))}
}
