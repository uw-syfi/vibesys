package supervisor

import (
	"context"
	"errors"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// ErrGone says a supervisor cannot be reached and will not answer again: it exited, was killed,
// or was released. A job whose supervisor is gone without an outcome is lost.
var ErrGone = errors.New("the job's supervisor is gone")

// Checkouts makes the clean checkout a command runs in. The supervisor asks for one per job and
// removes it when the command ends.
type Checkouts interface {
	// Checkout returns a clean checkout of the commit of the repository, and the function that
	// removes it. It fails with core.FailureNotFound for a commit the repository does not have
	// and core.FailureUnavailable for a repository it cannot reach.
	Checkout(ctx context.Context, repository string, commit core.Commit) (core.Checkout, func(), error)
}

// Handle is the daemon's end of one job's supervisor. Every method but Follow returns when the
// supervisor has answered. A method fails with ErrGone when the supervisor is gone, with the
// context's error when ctx is done, and with a *core.Failure otherwise.
type Handle interface {
	// Start lets the parked supervisor run the command. It is idempotent: on a supervisor that
	// started already it does nothing, and so a daemon that is not sure may call it again.
	Start(ctx context.Context) error

	// Cancel asks the supervisor to end the job's process group. It returns once the request is
	// recorded; the outcome says when the group has ended. The first reason wins, and a cancel
	// after the command ended changes nothing.
	Cancel(ctx context.Context, reason string) error

	// Follow passes the job's output from the byte offset to emit, in order and each byte once,
	// and returns the job's outcome after the command has ended and every byte was passed. It
	// waits as long as the command runs. A caller that comes back with the offset it already
	// holds loses nothing and gets nothing twice. If emit fails, Follow stops and returns its
	// error.
	Follow(ctx context.Context, offset uint64, emit func(data []byte) error) (lifecycle.Outcome, error)

	// Release ends the supervisor and removes what it kept. It fails with core.FailureConflict
	// for a running command, which is cancelled first and released after its outcome is recorded.
	// Releasing a parked supervisor aborts a job that never started. It is idempotent.
	Release(ctx context.Context) error
}

// Run is what is left of one launch, as a daemon that starts finds it.
type Run struct {
	Ref lifecycle.Ref
	// Handle reaches the supervisor, and is nil when Presence is not PresenceAlive.
	Handle Handle
	// Presence is PresenceAlive for a supervisor that answered, and PresenceGone for a run left
	// without one.
	Presence lifecycle.Presence
}

// Supervisors starts supervisors and finds the ones left from before.
type Supervisors interface {
	// Spawn writes the launch down, starts a parked supervisor for it, and returns a handle once
	// the supervisor answers. The command does not run until Handle.Start. On failure nothing is
	// left behind.
	Spawn(ctx context.Context, launch lifecycle.Launch) (Handle, error)

	// Runs returns every run left from an earlier daemon or launch, each probed once.
	Runs(ctx context.Context) ([]Run, error)

	// Forget removes what is left of a run whose supervisor is gone or released. It is
	// idempotent, and it fails with core.FailureConflict for a run whose supervisor still
	// answers.
	Forget(ctx context.Context, ref lifecycle.Ref) error
}
