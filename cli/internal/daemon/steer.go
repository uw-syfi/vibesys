package daemon

import (
	"context"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/runtimedir"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// drainFile is the app.DrainRecord of a daemon: the drain file of its runtime directory, so a daemon
// that restarts in the same directory drains as the last one did.
type drainFile struct{ dir runtimedir.Dir }

func (f drainFile) Load(ctx context.Context) (*core.Drain, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	m, err := f.dir.ReadDrain()
	if err != nil || m == nil {
		return nil, err
	}
	d, err := wire.ToDaemonDrain(m)
	if err != nil {
		return nil, err
	}
	return &d, nil
}

func (f drainFile) Save(ctx context.Context, d *core.Drain) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if d == nil {
		return f.dir.WriteDrain(nil)
	}
	return f.dir.WriteDrain(wire.DaemonDrain(*d))
}

// HoldJob implements cliv1.DaemonServiceServer.
func (s *Server) HoldJob(ctx context.Context, req *cliv1.HoldJobRequest) (*cliv1.HoldJobResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	job, err := s.app.HoldJob(ctx, call, core.JobID(req.GetJobId()), req.GetReason())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.HoldJobResponse{Job: wire.Job(job)}, nil
}

// ReleaseJob implements cliv1.DaemonServiceServer.
func (s *Server) ReleaseJob(ctx context.Context, req *cliv1.ReleaseJobRequest) (*cliv1.ReleaseJobResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	call, err := s.call(ctx, req.GetContext())
	if err != nil {
		return nil, wire.Error(err)
	}
	job, err := s.app.ReleaseJob(ctx, call, core.JobID(req.GetJobId()))
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ReleaseJobResponse{Job: wire.Job(job)}, nil
}

// Drain implements cliv1.DaemonServiceServer.
func (s *Server) Drain(ctx context.Context, req *cliv1.DrainRequest) (*cliv1.DrainResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	d, err := s.app.Drain(ctx, req.GetAgent(), req.GetReason())
	if err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.DrainResponse{Drain: wire.DaemonDrain(d)}, nil
}

// Resume implements cliv1.DaemonServiceServer.
func (s *Server) Resume(ctx context.Context, _ *cliv1.ResumeRequest) (*cliv1.ResumeResponse, error) {
	ctx, cancel := s.bound(ctx)
	defer cancel()
	if err := s.app.Resume(ctx); err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ResumeResponse{}, nil
}
