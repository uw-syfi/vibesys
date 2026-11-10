package flow

import (
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Bound is the longest a check or a measure job of the task can run once it has its machine: the
// sum of every limit its plan holds it to, so a job never takes longer than the bound says. It is
// what a job asks a standing allocation to have left (alloc.JobSubmitted.Bound). It is judged
// before the job's plan is made, so a measure counts its check whether or not a saved pass will
// make it skip it, and takes the samples the submission asks for, or the task's.
//
// The limits are the build, the check, every sample's benchmark, and the servers: in a task of two
// images each command starts the candidate's server and waits for it, and a job-scoped server is
// started once, with the task's reset before every sample.
func Bound(t task.Task, s core.Settings) time.Duration {
	var d time.Duration
	if t.Build != nil {
		d += t.Build.Timeout
	}
	samples := uint32(0)
	each := t.Benchmark.Timeout
	if m, ok := s.(core.MeasureSettings); ok {
		if samples = m.Samples; samples == 0 {
			samples = t.Samples
		}
	}
	if t.Service != nil {
		d += t.Service.StartupTimeout
		each += t.Service.Reset.Timeout
	}
	d += t.Check.Timeout + time.Duration(samples)*each
	if t.Candidate != nil {
		d += t.Candidate.StartupTimeout * time.Duration(1+samples)
	}
	return d
}
