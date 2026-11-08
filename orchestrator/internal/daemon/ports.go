package daemon

import (
	"context"
	"errors"
	"fmt"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/search"
)

// MergeWindow is how often the daemon ends the hub's merge window. Run ticks at this rate.
const MergeWindow = 50 * time.Millisecond

// MaxBackoffTicks is the most ticks Run skips between tries of a step that keeps failing: 40 ticks
// of MergeWindow are two seconds.
const MaxBackoffTicks = 40

// Clock tells the time. Production reads the system clock; a test sets one.
type Clock interface{ Now() time.Time }

// Nonces mints the nonce of each input. Two calls never return the same value, across restarts.
type Nonces interface{ Next() core.Nonce }

// Bounds gives each call the daemon makes outside itself a limit, so that one call that never
// returns cannot stall the loop that serves every agent. Production returns a context with a
// timeout; a test returns one it ends itself. The daemon asks for a bound for every call to a
// Sidecars, Sidecar or JobSource, and treats the end of the bound as a failure to retry:
// the call may or may not have had its effect, which every call it makes can bear.
type Bounds interface {
	Bound(ctx context.Context) (context.Context, context.CancelFunc)
}

// ErrExpired says a call outlived its bound. It is retryable, and says nothing of whether the call
// had its effect.
var ErrExpired = errors.New("daemon: the call outlived its bound")

// bound runs call under a bound. An error caused by the bound ending, and not by the caller's
// context, wraps ErrExpired.
func bound[T any](b Bounds, ctx context.Context, call func(context.Context) (T, error)) (T, error) {
	bctx, cancel := b.Bound(ctx)
	defer cancel()
	v, err := call(bctx)
	if err != nil && bctx.Err() != nil && ctx.Err() == nil {
		return v, fmt.Errorf("%w: %w", ErrExpired, err)
	}
	return v, err
}

func bounded(b Bounds, ctx context.Context, call func(context.Context) error) error {
	_, err := bound(b, ctx, func(c context.Context) (struct{}, error) { return struct{}{}, call(c) })
	return err
}

// ErrSidecarGone says a session's sidecar does not exist: it never started, or it died.
var ErrSidecarGone = errors.New("daemon: the session's sidecar is gone")

// GoneError is ErrSidecarGone with the reason the sidecar is known to be gone, which the session's
// loss then carries. A bare ErrSidecarGone says only that it is.
type GoneError struct{ Reason string }

func (e *GoneError) Error() string { return ErrSidecarGone.Error() + ": " + e.Reason }

// Is makes a *GoneError an ErrSidecarGone.
func (e *GoneError) Is(target error) bool { return target == ErrSidecarGone }

// lostBecause is what a session's loss says: what the daemon was doing, and the reason the sidecar
// gave for being gone when it gave one.
func lostBecause(doing string, err error) string {
	var gone *GoneError
	if errors.As(err, &gone) {
		return doing + ": " + gone.Reason
	}
	return doing
}

// ErrIdle is what an EventStream returns when it has nothing ready. The caller asks again on a
// later step. A real stream, which blocks in a goroutine of its own, hands the daemon what it has
// buffered and returns ErrIdle when the buffer is empty, so the daemon never waits on one.
var ErrIdle = errors.New("daemon: no sidecar event is ready")

// ModelPolicySource reads the host's model policy at the moment it is asked, so that a change to it
// takes effect on the next start with no restart. A policy that cannot be read is a Closed one, never
// an empty one.
type ModelPolicySource interface {
	ModelPolicy() core.ModelPolicy
}

// SessionStart is everything a session is started from: the effect the core decided, and what the
// run says besides. The core does not store the rest with the effect, so a launch retried after a
// restart reads the run as it is.
type SessionStart struct {
	Launch     core.LaunchSession
	Repository string
	Task       string
	JobTool    core.JobToolEndpoint
	// Tools is what the session's tool server is told: who it speaks for and how to reach the
	// daemon as that agent. It holds the session's token, which goes to the tool server and nowhere else.
	Tools *pb.ToolServerCredentials
}

// Sidecars starts sessions and finds the ones already running. How a session is held, and everything
// said between the host and what holds it before it opens, is behind this: the daemon sees a session
// start, and then opened or failed with a reason.
type Sidecars interface {
	// Launch starts a session, or returns the one already running for it, so a launch delivered
	// twice starts one. ErrSidecarGone is not an error of Launch: any error means the launch failed.
	Launch(ctx context.Context, start SessionStart) (Sidecar, error)
	// Attach finds a session that was launched before this daemon started. It returns ErrSidecarGone
	// when there is none, and a *LaunchError when one is there and cannot be used.
	Attach(ctx context.Context, session core.Session) (Sidecar, error)
}

// LaunchError is what Launch and Attach return when they know why a session cannot be used: the
// reason goes to the core with the error's text. Any other error from Launch is read as
// core.StartOther.
type LaunchError struct {
	Reason core.StartFailure
	Err    error
}

func (e *LaunchError) Error() string { return e.Err.Error() }
func (e *LaunchError) Unwrap() error { return e.Err }

// Sidecar is the daemon's side of one session's AgentSessionService.
type Sidecar interface {
	// Send carries out a command. A repeated effect id answers as the first delivery did. It
	// returns ErrSidecarGone when the sidecar is no longer there.
	Send(ctx context.Context, cmd *pb.SessionCommand) error
	// Follow streams the session's events after an offset. Its context bounds the call that opens the
	// stream and not the stream, which outlives it and ends with Close.
	Follow(ctx context.Context, afterOffset uint64) (EventStream, error)
	// Kill ends the process without waiting for it. It is for a session written off.
	Kill(ctx context.Context) error
}

// EventStream is one Follow. Next returns ErrIdle when nothing is ready, ErrSidecarGone when the
// sidecar ended, and any other error when the stream broke and may be reopened.
type EventStream interface {
	Next() (*pb.SessionEvent, error)
	Close()
}

// JobLog reads the job log of a run's task at the moment it is asked, not from a view kept earlier.
// The kept view follows a stream and may be behind it: a result recorded before a check-in's window
// ended but delivered after its facts froze would be in no window. A read made after the window ended
// has every result recorded before it, so the facts are worked out from one.
type JobLog interface {
	// ReadJobLog returns the whole job log of the run's task as the job service has it now. A log that
	// cannot be read is a view with its reason, as a kept view has; an error is a read that did not
	// finish.
	ReadJobLog(ctx context.Context, run core.Run) (search.View, error)
}

// Checkouts removes the working copies that agents made of a repository once their run is over.
type Checkouts interface {
	// Release removes the working copy of each agent that has one and holds no change to a tracked file; untracked files are build output, not work. A commit
	// such a copy made that no branch holds is first kept under a ref of the run. It
	// returns the ones it left in place, because removing them would lose work, or because it could
	// not tell. Repeating it is harmless: a working copy that is gone is not kept.
	Release(ctx context.Context, repository string, run core.RunID, agents []core.AgentID) ([]core.Checkout, error)
}

// StartChecks refuses a StartRun that cannot work, before it is decided: no run, event or receipt
// is made for a refused start. It is the daemon's only look at the repository and at whether
// agents can be launched at all, so the loop does no file I/O of its own.
type StartChecks interface {
	// CheckStart returns the canonical form of repository (symbolic links resolved, so that one
	// directory is one repository whichever way it was named) when a run of task in it can start,
	// and a *StartRefusal when it cannot. A repository that cannot feed the subagents' copies of it
	// is refused: one with no commit, or one whose task is not committed. It may be called for every
	// start and must be cheap.
	CheckStart(repository, task string) (canonical string, err error)
	// CheckRepository is the part of CheckStart that needs the repository alone, so a client can ask
	// while the task is still being typed: it refuses a daemon that cannot launch and a repository
	// that is not an absolute path to the root of a git work tree, and returns the canonical form.
	CheckRepository(repository string) (canonical string, err error)
	// Tasks names the tasks a repository holds, sorted: the folders under its .vibesys/tasks. It is
	// asked of the canonical form CheckRepository returned.
	Tasks(repository string) ([]string, error)
}

// Directories lists one folder of the host's file system for a path being typed. It reads the
// folder's own children and never what is below them.
type Directories interface {
	// List returns the entries of the folder path names (absolute, or starting with ~ for the host's
	// home), sorted by name, with the entries whose names start with a dot only when hidden. A folder
	// with more entries than the cap, or one still being read when ctx ends, comes back with what was
	// read and Truncated set. A path that cannot be listed is a *DirectoryError.
	List(ctx context.Context, path string, hidden bool) (Listing, error)
}

// Listing is the answer of Directories.List.
type Listing struct {
	Entries   []DirectoryEntry
	Truncated bool
}

// DirectoryEntry is one child of a listed folder.
type DirectoryEntry struct {
	Name string
	// IsDirectory is a directory, or a link to one.
	IsDirectory bool
	// IsGitRepository is a directory that holds a .git.
	IsGitRepository bool
	// HasTasks is a directory with at least one task folder under .vibesys/tasks.
	HasTasks bool
}

// DirectoryFault says why a folder could not be listed.
type DirectoryFault int

const (
	// DirectoryNotAbsolute is a path that is neither absolute nor starts with ~/.
	DirectoryNotAbsolute DirectoryFault = iota + 1
	// DirectoryNotFound is a path that names nothing.
	DirectoryNotFound
	// DirectoryNotADirectory is a path that names a file.
	DirectoryNotADirectory
	// DirectoryDenied is a folder the daemon may not read.
	DirectoryDenied
)

// DirectoryError is a path that cannot be listed, in words that name it.
type DirectoryError struct {
	Fault  DirectoryFault
	Reason string
}

func (e *DirectoryError) Error() string { return e.Reason }

// StartRefusal says why a start was refused, in words that name the offending path or setting.
type StartRefusal struct {
	Reason string
	// NotReady says the daemon, and not the request, is at fault: no agent can launch whatever the
	// repository holds. Otherwise the repository or the task is wrong.
	NotReady bool
}

func (r *StartRefusal) Error() string { return r.Reason }
