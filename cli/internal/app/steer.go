package app

import (
	"context"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
	"github.com/uw-syfi/vibesys/cli/internal/store"
)

// Steering the queue: an operator, or a lead agent, holds and releases queued jobs and drains the
// daemon, on top of what the scheduler decides from the resources. Who asks is the caller's own
// word (CallContext.Agent); the daemon checks no right to steer.
//
// A hold is a fact of its job, kept in its store, and a drain a fact of the daemon, kept by the
// DrainRecord. Each is written there first and then told to the machine, so the machine never acts
// on a change that is not durable, and a daemon that starts again takes both up before it starts any
// job (Start).

// DrainRecord keeps whether the daemon drains, across its restarts: the daemon's runtime directory
// in the shipped binary. Load returns nil when the daemon does not drain, and Save(nil) ends a
// drain. A Save that returned is what the next Load returns, in this daemon or the next.
type DrainRecord interface {
	Load(ctx context.Context) (*core.Drain, error)
	Save(ctx context.Context, d *core.Drain) error
}

// MemoryDrainRecord is a DrainRecord kept in memory, for the life of the value: a test's record, and
// the one an App made with none uses, which drains until the App closes and no longer.
type MemoryDrainRecord struct {
	mu    sync.Mutex
	drain *core.Drain
}

// Load implements DrainRecord.
func (m *MemoryDrainRecord) Load(ctx context.Context) (*core.Drain, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	if m.drain == nil {
		return nil, nil
	}
	d := *m.drain
	return &d, nil
}

// Save implements DrainRecord.
func (m *MemoryDrainRecord) Save(ctx context.Context, d *core.Drain) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	m.drain = nil
	if d != nil {
		kept := *d
		m.drain = &kept
	}
	return nil
}

// steeringOf is how a job waits, as its store has it with its task's plans, for the machine.
func steeringOf(job core.Job, plans []core.Plan) lifecycle.Steering {
	return lifecycle.Steering{Hold: job.Hold, Priority: core.EffectivePriority(job, plans)}
}

// HoldJob holds a queued job: it keeps its place in the queue and is not started until it is
// released, and it stands in no other job's way. The hold is recorded on the job with who asked and
// why, and a note keeps it after a release. A job that is not queued is refused as a conflict. A job
// the daemon had begun to start already, but whose store did not have it running yet, starts, and
// is not held from then on.
func (a *App) HoldJob(ctx context.Context, c core.CallContext, id core.JobID, reason string) (core.Job, error) {
	if err := core.CheckReason("reason", reason); err != nil {
		return core.Job{}, err
	}
	return a.controlJob(ctx, c, id, core.HoldJob{Hold: core.JobHold{Agent: c.Agent, Reason: reason, At: a.now()}})
}

// ReleaseJob lets a held job start in its turn, which is its place in the queue as if it had never
// been held. A job that is not held is returned as it is.
func (a *App) ReleaseJob(ctx context.Context, c core.CallContext, id core.JobID) (core.Job, error) {
	return a.controlJob(ctx, c, id, core.ReleaseJob{Agent: c.Agent, At: a.now()})
}

// controlJob records a change to how a job waits in its store, then tells the machine.
func (a *App) controlJob(ctx context.Context, c core.CallContext, id core.JobID, control core.JobControl) (core.Job, error) {
	scope := c.Scope
	ctx = a.generationContext(ctx, storeIDOf(scope))
	st, err := a.open(ctx, scope)
	if err != nil {
		return core.Job{}, err
	}
	job, err := st.ControlJob(ctx, id, control)
	if err != nil {
		return core.Job{}, err
	}
	if err := a.steer(ctx, scope, st, []core.Job{job}); err != nil {
		return core.Job{}, err
	}
	return job, nil
}

// steer tells the machine how the jobs wait now, as the store has them, with the task's plans.
func (a *App) steer(ctx context.Context, scope core.Scope, st store.Store, jobs []core.Job) error {
	plans, err := st.Plans(ctx, core.PlanFilter{})
	if err != nil {
		return err
	}
	for _, job := range jobs {
		ref := lifecycle.Ref{Store: storeIDOf(scope), Job: job.ID}
		if err := a.settleEffect(ctx, message{event: lifecycle.Steered{Ref: ref, Steering: steeringOf(job, plans)}, changed: true}); err != nil {
			return err
		}
	}
	return nil
}

// SetJobPriority sets a queued job's own priority, which overrides its plans', or with no priority
// lets it inherit theirs again. A reason is required; the change is kept as a note on the job with
// who asked and when. A job that is not queued is refused as a conflict: no job that runs is ever
// stopped or reordered.
func (a *App) SetJobPriority(ctx context.Context, c core.CallContext, id core.JobID, priority core.Priority, reason string) (core.Job, error) {
	if priority != "" && !priority.Valid() {
		return core.Job{}, invalid("priority: %q is not a priority (low, normal or high)", priority)
	}
	if err := core.CheckPriorityReason("reason", reason); err != nil {
		return core.Job{}, err
	}
	return a.controlJob(ctx, c, id, core.SetJobPriority{Priority: priority, Reason: reason, Agent: c.Agent, At: a.now()})
}

// Drain makes the daemon start no queued job until Resume: the jobs that run go on, the queued ones
// keep their places, and each says the daemon drains. The drain is recorded first, so a daemon that
// restarts drains too. A second drain replaces the first, with its own agent, reason and time.
func (a *App) Drain(ctx context.Context, agent, reason string) (core.Drain, error) {
	if err := core.CheckReason("reason", reason); err != nil {
		return core.Drain{}, err
	}
	d := core.Drain{Agent: agent, Reason: reason, Since: a.now()}
	if err := a.cfg.Drains.Save(ctx, &d); err != nil {
		return core.Drain{}, recording("the drain", err)
	}
	if err := a.settle(message{event: lifecycle.Drained{Drain: &d}, changed: true}); err != nil {
		return core.Drain{}, err
	}
	return d, nil
}

// Resume ends a drain: the queued jobs start in their turn again. A daemon that does not drain is
// left as it is.
func (a *App) Resume(ctx context.Context) error {
	if err := a.cfg.Drains.Save(ctx, nil); err != nil {
		return recording("the end of the drain", err)
	}
	return a.settle(message{event: lifecycle.Drained{}, changed: true})
}

// recording is the failure to record what, which says what was not recorded.
func recording(what string, err error) error {
	f := failureOf(err)
	return &core.Failure{Kind: f.Kind, Message: "recording " + what + ": " + f.Message}
}
