package flowrun

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"path"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/flow"
	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
	"github.com/uw-syfi/vibesys/cli/internal/reading"
)

// Checkouts makes the clean checkout a job runs in. supervisor.Checkouts is one.
type Checkouts interface {
	// Checkout returns a clean checkout of the commit of the repository, and the function that
	// removes it.
	Checkout(ctx context.Context, repository string, commit core.Commit) (core.Checkout, func(), error)
}

// Config is what a Shell needs. Every field is required.
type Config struct {
	Repository  string
	Commit      core.Commit
	Environment environment.Environment
	Checkouts   Checkouts
	Files       hostfs.FS
	Clock       clock.Clock
	// Output receives the output of every command and a line before each saying what runs.
	Output io.Writer
	// Artifacts, when set, is the job's artifacts directory: an absolute path that outlives the
	// job. Each command gets its own directory in it, numbered in the order they run.
	Artifacts string
}

// Shell carries out the requests of one job's flow. Create it with New and close it when the job
// ends. It is for one flow at a time.
type Shell struct {
	cfg Config

	mu       sync.Mutex
	checkout core.Checkout
	scratch  string
	removals []func()
	runs     int // the commands run so far
}

var _ flow.Shell = (*Shell)(nil)

// New returns a shell that has made nothing yet.
func New(cfg Config) *Shell { return &Shell{cfg: cfg} }

// Run drives the plan to its verdict on a new shell and closes the shell. It fails only for a
// plan that is not valid.
func Run(ctx context.Context, cfg Config, p flow.Plan) (flow.Verdict, error) {
	sh := New(cfg)
	defer sh.Close()
	return flow.Drive(ctx, p, sh)
}

// Close removes the scratch directory and then the checkout. It is idempotent.
func (s *Shell) Close() {
	s.mu.Lock()
	removals := s.removals
	s.removals = nil
	s.mu.Unlock()
	for i := len(removals) - 1; i >= 0; i-- {
		removals[i]()
	}
}

// Do implements flow.Shell.
func (s *Shell) Do(ctx context.Context, r flow.Request) flow.Event {
	switch r := r.(type) {
	case flow.Checkout:
		return s.checkoutAndScratch(ctx)
	case flow.Run:
		return s.run(ctx, r)
	case flow.Observe:
		return s.observe(ctx)
	case flow.BuildCandidate:
		return s.buildCandidate(ctx, r)
	}
	return flow.CheckoutFailed{Failure: core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the shell cannot carry out %T", r)}}
}

func (s *Shell) checkoutAndScratch(ctx context.Context) flow.Event {
	checkout, remove, err := s.cfg.Checkouts.Checkout(ctx, s.cfg.Repository, s.cfg.Commit)
	if err != nil {
		return s.checkoutFailed(ctx, err)
	}
	scratch, removeScratch, err := s.cfg.Files.MakeScratch(ctx)
	if err != nil {
		remove()
		return s.checkoutFailed(ctx, err)
	}
	s.mu.Lock()
	s.checkout, s.scratch = checkout, scratch
	s.removals = append(s.removals, remove, removeScratch)
	s.mu.Unlock()
	return flow.CheckedOut{Dir: checkout.Dir, Scratch: scratch}
}

// checkoutFailed answers a failed checkout: a cancel is a cancel, and anything else says why.
func (s *Shell) checkoutFailed(ctx context.Context, err error) flow.Event {
	if ctx.Err() != nil && errors.Is(err, context.Canceled) {
		return flow.Cancelled{}
	}
	return flow.CheckoutFailed{Failure: failureOf(err)}
}

func (s *Shell) run(ctx context.Context, r flow.Run) flow.Event {
	s.mu.Lock()
	checkout, scratch := s.checkout, s.scratch
	s.runs++
	n := s.runs
	s.mu.Unlock()
	_, _ = fmt.Fprintf(s.cfg.Output, "[%s] %s\n", r.Stage, strings.Join(r.Argv, " "))
	x := core.Execution{Checkout: checkout, Scratch: scratch, Argv: r.Argv, Timeout: r.Timeout, Measured: r.Stage == flow.StageBenchmark, Candidate: r.Candidate, Later: r.Later}
	if s.cfg.Artifacts != "" {
		x.Artifacts = ArtifactsOf(s.cfg.Artifacts, n, r.Stage)
		if err := s.cfg.Files.MakeDir(ctx, x.Artifacts); err != nil {
			return flow.NotRan{Failure: core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("making the artifacts directory: %v", err)}}
		}
		_, _ = fmt.Fprintf(s.cfg.Output, "[%s] artifacts: %s\n", r.Stage, x.Artifacts)
	}
	started := s.cfg.Clock.Now()
	attempt := s.cfg.Environment.Run(ctx, x, s.cfg.Output)
	ended := s.cfg.Clock.Now()
	var ran flow.Ran
	switch a := attempt.(type) {
	case core.NotStarted:
		return flow.NotRan{Failure: a.Failure}
	case core.Unobserved:
		return flow.Unseen{Failure: a.Failure}
	case core.Ended:
		ran = flow.Ran{Exec: a.Exec, ServiceLog: a.ServiceLog}
	default:
		return flow.Unseen{Failure: core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("the environment answered %T", attempt)}}
	}
	ran.Started, ran.Ended = started, ended
	if r.Read != "" {
		ran.Output, ran.Found, ran.ReadError = s.read(r.Read)
	}
	return ran
}

// buildCandidate builds the candidate's image of a task of two images, or finds it built. The
// build's progress goes to the job's output.
func (s *Shell) buildCandidate(ctx context.Context, r flow.BuildCandidate) flow.Event {
	_, _ = fmt.Fprintf(s.cfg.Output, "[%s] the image of %s, context %s\n", flow.StageCandidate, r.Build.Dockerfile, r.Build.Context)
	img, err := s.cfg.Environment.Candidate(ctx, r.Build, s.cfg.Output)
	if err != nil {
		if ctx.Err() != nil && errors.Is(err, context.Canceled) {
			return flow.Cancelled{}
		}
		return flow.NotRan{Failure: failureOf(err)}
	}
	return flow.CandidateBuilt{Image: img}
}

// ArtifactsOf is the artifacts directory of the nth command of a job (from 1), of the stage: the
// commands' directories sort in the order they ran.
func ArtifactsOf(job string, n int, stage flow.Stage) string {
	return path.Join(job, fmt.Sprintf("%02d-%s", n, stage))
}

// read reads the result file a command was asked to write. A file that is not there is not an
// error here: only a command that exited 0 owes one, and the machine decides.
func (s *Shell) read(path string) (data []byte, found bool, problem string) {
	data, err := s.cfg.Files.ReadFile(path, reading.MaxFileBytes)
	switch {
	case err == nil:
		return data, true, ""
	case errors.Is(err, fs.ErrNotExist):
		return nil, false, ""
	case errors.Is(err, hostfs.ErrTooLarge):
		return nil, true, fmt.Sprintf("it is larger than %d bytes", reading.MaxFileBytes)
	default:
		return nil, true, err.Error()
	}
}

// observe reads the machine the environment's commands run on. What cannot be read stays empty:
// the facts and the load are recorded and never acted on.
func (s *Shell) observe(ctx context.Context) flow.Event {
	var o flow.Observed
	if m, err := s.cfg.Environment.Machine(ctx); err == nil {
		o.Machine = m
	}
	if l, err := s.cfg.Environment.Load(ctx); err == nil {
		o.Load = l
	}
	return o
}

func failureOf(err error) core.Failure {
	var f *core.Failure
	if errors.As(err, &f) {
		return *f
	}
	return core.Failure{Kind: core.FailureInternal, Message: err.Error()}
}
