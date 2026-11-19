package app

import (
	"context"
	"errors"
	"fmt"
	"io"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// OutputFiles opens the files command jobs kept as their outputs, where the machine keeps them.
// supervisor.Host is one.
type OutputFiles interface {
	// OpenOutput opens the kept file at path of the job of the task in the repository. It refuses a
	// path that leaves the job's directory and a file that is not a regular one.
	OpenOutput(repository, task string, job core.JobID, path string) (io.ReadCloser, error)
}

// outputChunk is the most bytes of an output file passed at once.
const outputChunk = 1 << 20

// OutputChunk is a piece of one output file, at an offset in it.
type OutputChunk struct {
	Path   string
	Offset uint64
	Data   []byte
}

// ReadOutputs passes a command job that kept outputs to emitJob, and with content then each kept
// file's bytes, in the order the job lists them, to emitChunk. It refuses a job that kept none,
// saying why: it did not declare outputs (core.FailureInvalidInput), has not ended, ended before
// its command left anything, or its outputs were refused (core.FailureConflict). A file whose
// bytes are not what the job recorded is refused as core.FailureConflict once it is read past its
// size; the reader also holds what it gets to the recorded digests.
func (a *App) ReadOutputs(ctx context.Context, scope core.Scope, id core.JobID, content bool, emitJob func(core.Job) error, emitChunk func(OutputChunk) error) error {
	st, err := a.open(ctx, scope)
	if err != nil {
		return err
	}
	job, err := a.readJob(ctx, st, id)
	if err != nil {
		return err
	}
	kept, err := keptOutputs(job)
	if err != nil {
		return err
	}
	if err := emitJob(job); err != nil {
		return err
	}
	if !content {
		return nil
	}
	if a.cfg.Outputs == nil {
		return &core.Failure{Kind: core.FailureUnavailable, Message: "this daemon cannot read output files"}
	}
	for _, f := range kept.Files {
		if err := a.sendOutput(scope, id, f, emitChunk); err != nil {
			return err
		}
	}
	return nil
}

// keptOutputs is the files a job kept, or why it has none to read.
func keptOutputs(job core.Job) (core.OutputsKept, error) {
	command, ok := job.Spec.Settings.(core.CommandSettings)
	if !ok || !command.Outputs {
		return core.OutputsKept{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(
			"job %s keeps no outputs: only a command job submitted with --outputs does", job.ID)}
	}
	switch o := job.Outputs.(type) {
	case core.OutputsKept:
		return o, nil
	case core.OutputsRefused:
		return core.OutputsKept{}, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s kept no outputs: %s", job.ID, o.Reason)}
	}
	if !job.State.Final() {
		return core.OutputsKept{}, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
			"job %s is %s; its outputs are recorded when its command ends", job.ID, job.State)}
	}
	return core.OutputsKept{}, &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf(
		"job %s kept no outputs: it ended %s before its command left any to record (see vibesys-cli logs %s)", job.ID, job.State, job.ID)}
}

// sendOutput passes one kept file, never more of it than the job recorded.
func (a *App) sendOutput(scope core.Scope, id core.JobID, f core.OutputFile, emit func(OutputChunk) error) error {
	r, err := a.cfg.Outputs.OpenOutput(string(scope.Repository()), string(scope.Task()), id, f.Path)
	if err != nil {
		var failure *core.Failure
		if errors.As(err, &failure) {
			return failure
		}
		return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s's output %q cannot be read where it was kept: %v", id, f.Path, err)}
	}
	defer r.Close()
	limited := io.LimitReader(r, int64(f.Size)+1)
	buf := make([]byte, outputChunk)
	var offset uint64
	for {
		n, err := io.ReadFull(limited, buf)
		if n > 0 {
			if offset+uint64(n) > f.Size {
				return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s's output %q is larger than the %d bytes it kept: it changed since", id, f.Path, f.Size)}
			}
			if err := emit(OutputChunk{Path: f.Path, Offset: offset, Data: append([]byte(nil), buf[:n]...)}); err != nil {
				return err
			}
			offset += uint64(n)
		}
		switch {
		case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF):
			if offset != f.Size {
				return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("job %s's output %q has %d of the %d bytes it kept: it changed since", id, f.Path, offset, f.Size)}
			}
			return nil
		case err != nil:
			return &core.Failure{Kind: core.FailureConflict, Message: fmt.Sprintf("reading job %s's output %q: %v", id, f.Path, err)}
		}
	}
}
