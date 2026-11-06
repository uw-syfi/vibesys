package docker

import (
	"context"
	"fmt"
	"io"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Lab implements environment.Environment: it starts the containers a run of the execution would
// have, with nothing in them, and hands them to hold. With a candidate's image they are the two
// containers of a task of two images, the candidate's with the placement's GPUs and the task's in
// its network, as for a profile; otherwise the one container is of the task's image. They are
// placed, staged and removed exactly as Run's, so a lab of two images on an apptainer placement
// holds one allocation, on one node, for the two of them, and a placement that runs no task of two
// images (one with no probe program, as an ssh or a slurm placement has) holds no lab of one.
func (e *env) Lab(ctx context.Context, x core.Execution, out io.Writer, hold environment.HoldLab) core.Attempt {
	if err := environment.ValidateLab(x); err != nil {
		return core.NotStarted{Failure: core.AsFailure(err)}
	}
	if ctx.Err() != nil {
		return core.Ended{Exec: core.ExecOutcome{Kind: core.ExecCancelled}}
	}
	if out == nil {
		out = io.Discard
	}
	r := &run{e: e, ctx: ctx, x: x, sink: environment.NewSink(out), tail: &environment.Tail{}}
	attempt := r.prepareAndHold(hold)
	return environment.Finish(ctx, attempt, r.end(false))
}

// prepareAndHold makes the lab's containers and holds them until hold returns. Until hold is
// called nothing of the lab ran: its containers ran only what keeps them up, and their checks.
func (r *run) prepareAndHold(hold environment.HoldLab) core.Attempt {
	notStarted := func(err error) core.Attempt { return core.NotStarted{Failure: core.AsFailure(err)} }
	p := r.e.p
	spec, err := r.e.spec(r.ctx)
	if err != nil {
		return notStarted(err)
	}
	id := p.cfg.IDs.New()
	if err := checkRunID(id); err != nil {
		return notStarted(err)
	}
	cand := r.x.Candidate
	if cand != nil {
		// A placement that runs no task of two images holds no lab of one either: the two
		// containers are a run's, and the task's has the probe program as a run's has.
		if err := r.canServe(); err != nil {
			return notStarted(err)
		}
		if _, err := r.claim(containerName(id) + candidateSuffix); err != nil {
			return notStarted(err)
		}
	}
	if _, err := r.claim(containerName(id)); err != nil {
		return notStarted(err)
	}
	if err := p.sweep(r.ctx); err != nil {
		return notStarted(err)
	}
	img, err := p.image(r.ctx, spec, io.Discard)
	if err != nil {
		return notStarted(err)
	}
	rooms := map[core.LabRoom]*box{}
	if cand == nil {
		only, err := r.containerSpec(r.boxes[0], id, img, roleOnly, "")
		if err != nil {
			return notStarted(err)
		}
		if err := r.start(r.boxes[0], only); err != nil {
			return notStarted(err)
		}
		rooms[core.LabCandidate] = r.boxes[0]
	}
	var port int
	if cand != nil {
		candImg, ok, err := p.cfg.Engine.Image(r.ctx, cand.Image)
		if err == nil && !ok {
			err = &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("docker: the engine has no candidate image %s; it is built before the lab", cand.Image)}
		}
		if candImg, err = checked(candImg, err); err != nil {
			return notStarted(err)
		}
		server, err := r.containerSpec(r.boxes[0], id, candImg, roleServer, "")
		if err != nil {
			return notStarted(err)
		}
		if err := r.start(r.boxes[0], server); err != nil {
			return notStarted(err)
		}
		if port, err = r.boxes[0].container.Port(r.ctx, id); err != nil {
			return notStarted(err)
		}
		beside, err := r.containerSpec(r.boxes[1], id, img, roleBeside, core.ServiceURL(port))
		if err != nil {
			return notStarted(err)
		}
		if err := r.start(r.boxes[1], beside); err != nil {
			return notStarted(err)
		}
		rooms[core.LabCandidate], rooms[core.LabTask] = r.boxes[0], r.boxes[1]
	} else if port, err = r.boxes[0].container.Port(r.ctx, id); err != nil {
		return notStarted(err)
	}
	limit, stop := p.cfg.Timers.Timeout(r.ctx, r.x.Timeout)
	r.stopLimit = append(r.stopLimit, stop)
	for _, b := range r.boxes {
		if attempt := r.check(b, limit); attempt != nil {
			return attempt
		}
	}
	local := Dirs{Checkout: r.x.Checkout.Dir, Scratch: r.x.Scratch, Artifacts: r.x.Artifacts}
	bench := environment.Bench{
		Rooms: map[core.LabRoom]environment.Launcher{}, Port: port, Artifacts: ArtifactsDir, Shell: true,
		Limit: limit, Cancelled: r.ctx.Done(),
		Grace:    func() (<-chan struct{}, func()) { return p.cfg.Timers.Timeout(r.ctx, p.cfg.Grace) },
		Collect:  func(ctx context.Context) error { return p.cfg.Machine.Collect(ctx, local, *r.staged) },
		Replaced: func(ctx context.Context) error { return p.cfg.Machine.Replace(ctx, local, *r.staged) },
		Added: func(ctx context.Context, rel core.LabPath) error {
			return p.cfg.Machine.Add(ctx, local, *r.staged, string(rel))
		},
	}
	for room, b := range rooms {
		bench.Rooms[room] = b.container
	}
	return core.Ended{Exec: hold(bench)}
}
