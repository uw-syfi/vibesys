package flowrun

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/backend"
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
	Backend     backend.Backend
	Files       hostfs.FS
	Clock       clock.Clock
	// Output receives the output of every command and a line before each saying what runs.
	Output io.Writer
}

// Shell carries out the requests of one job's flow. Create it with New and close it when the job
// ends. It is for one flow at a time.
type Shell struct {
	cfg Config

	mu       sync.Mutex
	checkout core.Checkout
	scratch  string
	removals []func()
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
	s.mu.Unlock()
	_, _ = fmt.Fprintf(s.cfg.Output, "[%s] %s\n", r.Stage, strings.Join(r.Argv, " "))
	started := s.cfg.Clock.Now()
	exec, err := s.cfg.Environment.Run(ctx, core.Execution{Checkout: checkout, Scratch: scratch, Argv: r.Argv, Timeout: r.Timeout}, s.cfg.Output)
	ended := s.cfg.Clock.Now()
	if err != nil {
		if ctx.Err() != nil && errors.Is(err, context.Canceled) {
			return flow.Ran{Exec: core.ExecOutcome{Kind: core.ExecCancelled}, Started: started, Ended: ended}
		}
		return flow.NotRan{Failure: failureOf(err)}
	}
	ran := flow.Ran{Exec: exec, Started: started, Ended: ended}
	if r.Read != "" {
		ran.Output, ran.Found, ran.ReadError = s.read(r.Read)
	}
	return ran
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

// observe reads the machine. What cannot be read stays empty: the facts and the load are
// recorded and never acted on.
func (s *Shell) observe(ctx context.Context) flow.Event {
	var o flow.Observed
	if m, err := s.cfg.Backend.Machine(ctx); err == nil {
		o.Machine = m
	}
	if l, err := s.cfg.Backend.Load(ctx); err == nil {
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
