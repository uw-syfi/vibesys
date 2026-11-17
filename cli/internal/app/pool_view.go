package app

import (
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/schedule"
)

// PoolView is a placement's pool of GPUs as the daemon last saw it: how many devices there are and
// how many are free, which jobs hold the others, and which jobs wait for some.
type PoolView struct {
	// Pool names the pool, by the machine whose GPUs they are, such as gpu:local.
	Pool    core.PoolID
	Size    int
	Free    int
	Holders []PoolHolder
	// Waiting are the jobs that wait for devices of the pool, in the order they are served.
	Waiting []PoolWaiter
}

// PoolHolder is a job that holds devices of a pool.
type PoolHolder struct {
	Job   core.JobID
	Kind  core.JobKind
	Run   core.RunID
	Grant core.Grant
}

// PoolWaiter is a job that waits for devices of a pool, and how many it claims.
type PoolWaiter struct {
	Job    core.JobID
	Wanted int
}

// PoolOf is the pool of GPUs of a placement of the operator config, and false for a placement
// whose GPUs the daemon does not hand out by number (it has no count, or a cluster hands them out).
func (a *App) PoolOf(placement string) (PoolView, bool) {
	pooled, ok := a.cfg.Scheduler.Demand.(schedule.Pooled)
	if !ok {
		return PoolView{}, false
	}
	pool := pooled.PoolOf(placement)
	if pool == nil {
		return PoolView{}, false
	}
	v := a.view.Load()
	out := PoolView{Pool: core.PoolID(pool.Resource), Size: pool.Size, Free: max(pool.Size-v.used[pool.Resource], 0)}
	for _, h := range v.holders {
		if h.Grant.Pool == out.Pool {
			out.Holders = append(out.Holders, PoolHolder{Job: h.Ref.Job, Kind: h.Work.Kind, Run: h.Work.Run, Grant: h.Grant})
		}
	}
	for _, q := range v.waiting {
		if wanted, _ := a.poolShare(pool.Resource, q.Work, v.used); wanted > 0 {
			out.Waiting = append(out.Waiting, PoolWaiter{Job: q.Ref.Job, Wanted: wanted})
		}
	}
	return out, true
}
