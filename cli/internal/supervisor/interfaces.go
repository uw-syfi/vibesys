package supervisor

import (
	"context"
	"errors"
	"fmt"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/lifecycle"
)

// ErrGone says a supervisor cannot be reached and looks gone. On its own it proves nothing: only a
// Gone, which carries the proof, makes a job lost.
var ErrGone = errors.New("the job's supervisor is gone")

// Gone says a supervisor is gone and how that is known, and what is known of whether its command
// was told to start. It is an ErrGone.
type Gone struct {
	Proof lifecycle.DeathProof
	Start lifecycle.Start
}

func (g Gone) Error() string { return fmt.Sprintf("%v: %s", ErrGone, g.Proof) }

// Is says a Gone is an ErrGone.
func (Gone) Is(target error) bool { return target == ErrGone }

// Checkouts makes the clean checkout a command runs in. The supervisor asks for one per job and
// removes it when the command ends.
type Checkouts interface {
	// Checkout returns a clean checkout of the commit of the repository, and the function that
	// removes it. It fails with core.FailureNotFound for a commit the repository does not have
	// and core.FailureUnavailable for a repository it cannot reach.
	Checkout(ctx context.Context, repository string, commit core.Commit) (core.Checkout, func(), error)
}

// Handle is the daemon's end of one job's supervisor. Every method but Follow returns when the
// supervisor has answered. A method fails with a Gone when the supervisor is proved gone, with
// ErrGone when it only looks it, with the context's error when ctx is done, and with a
// *core.Failure otherwise. A supervisor that is dying can be a *core.Failure until its death is
// proved: asking again gives the Gone.
type Handle interface {
	// Start lets the parked supervisor run the command. It is idempotent: on a supervisor that
	// started already it does nothing, and so a daemon that is not sure may call it again. Before it
	// asks, it marks the run durably as told to start, so that a run found dead without the mark is
	// known never to have started (see Dead.Start); it fails, asking nothing, when it cannot.
	Start(ctx context.Context) error

	// Cancel asks the supervisor to end the job's process group. It returns once the request is
	// recorded; the outcome says when the group has ended. The first reason wins, and a cancel
	// after the command ended changes nothing.
	Cancel(ctx context.Context, reason string) error

	// Follow passes the job's output from the byte offset to emit, in order and each byte once,
	// and returns the job's outcome after the command has ended and every byte was passed. It
	// waits as long as the command runs. A caller that comes back with the offset it already
	// holds loses nothing and gets nothing twice. An offset past the end passes nothing, which is
	// how a caller learns only the outcome. If emit fails, Follow stops and returns its error.
	Follow(ctx context.Context, offset uint64, emit func(data []byte) error) (lifecycle.Outcome, error)

	// Release ends the supervisor and removes what it kept. It fails with core.FailureConflict
	// for a running command, which is cancelled first and released after its outcome is recorded.
	// Releasing a parked supervisor aborts a job that never started. It is idempotent.
	Release(ctx context.Context) error

	// Close lets go of the connection and nothing else: the supervisor is not told, and goes on.
	// It is idempotent, and a call in flight fails.
	Close()
}

// Run is what is left of one job's launch, as a daemon finds it. The set is closed. Alive is the
// only one with a handle, which its receiver closes; Dead is the only one that proves anything.
type Run interface {
	// Job is the job the run is for.
	Job() lifecycle.Ref
	// Started returns what the run was started with, which says what store its job is in, and
	// false when that is not known.
	Started() (lifecycle.Launch, bool)
	// Found says what the run means to the machine.
	Found() lifecycle.Presence
	run()
}

// Alive is a supervisor that answered.
type Alive struct {
	Ref    lifecycle.Ref
	Handle Handle
	Launch lifecycle.Launch
	Phase  lifecycle.RunPhase
}

// Dead is a run whose supervisor is proved gone. Launch is nil when its record cannot be read.
// Start is NeverStarted only when the run's record is read and says its command was never told to
// start; anything else may have started.
type Dead struct {
	Ref    lifecycle.Ref
	Proof  lifecycle.DeathProof
	Start  lifecycle.Start
	Launch *lifecycle.Launch
}

// Unknown is a run that may live and that the daemon cannot use, such as a supervisor of another
// build, or one whose directory went away while the machine stayed up. Launch is nil when its
// record cannot be read.
type Unknown struct {
	Ref    lifecycle.Ref
	Launch *lifecycle.Launch
}

// None says there is no sign of a run for the job.
type None struct{ Ref lifecycle.Ref }

// Job implements Run.
func (r Alive) Job() lifecycle.Ref { return r.Ref }

// Job implements Run.
func (r Dead) Job() lifecycle.Ref { return r.Ref }

// Job implements Run.
func (r Unknown) Job() lifecycle.Ref { return r.Ref }

// Job implements Run.
func (r None) Job() lifecycle.Ref { return r.Ref }

// Started implements Run.
func (r Alive) Started() (lifecycle.Launch, bool) { return r.Launch, true }

// Started implements Run.
func (r Dead) Started() (lifecycle.Launch, bool) { return started(r.Launch) }

// Started implements Run.
func (r Unknown) Started() (lifecycle.Launch, bool) { return started(r.Launch) }

// Started implements Run.
func (None) Started() (lifecycle.Launch, bool) { return lifecycle.Launch{}, false }

func started(l *lifecycle.Launch) (lifecycle.Launch, bool) {
	if l == nil {
		return lifecycle.Launch{}, false
	}
	return *l, true
}

// Found implements Run.
func (r Alive) Found() lifecycle.Presence {
	return lifecycle.RunAlive{Phase: r.Phase, Limit: r.Launch.Limit()}
}

// Found implements Run.
func (r Dead) Found() lifecycle.Presence { return lifecycle.RunDead{Proof: r.Proof, Start: r.Start} }

// Found implements Run.
func (r Unknown) Found() lifecycle.Presence {
	l, _ := r.Started()
	return lifecycle.RunUnknown{Limit: l.Limit()}
}

// Found implements Run.
func (None) Found() lifecycle.Presence { return lifecycle.RunNone{} }

func (Alive) run()   {}
func (Dead) run()    {}
func (Unknown) run() {}
func (None) run()    {}

// Supervisors starts supervisors and finds the ones left from before.
type Supervisors interface {
	// Spawn writes the launch down, starts a parked supervisor for it, and returns a handle once
	// the supervisor answers. The command does not run until Handle.Start. On failure nothing is
	// left behind.
	Spawn(ctx context.Context, launch lifecycle.Launch) (Handle, error)

	// Runs returns every run left from an earlier daemon or launch, each probed once. It fails
	// with the context's error when ctx is done, and never reports that as a dead supervisor.
	Runs(ctx context.Context) ([]Run, error)

	// Probe returns what is left of one job's run now, as Runs does: None for a job with no sign
	// of one.
	Probe(ctx context.Context, ref lifecycle.Ref) (Run, error)

	// Forget removes what is left of a run. The proof that its supervisor is gone is the only
	// thing that lets it, and a run with none cannot be forgotten. It is idempotent. It fails with
	// core.FailureConflict for a run whose supervisor may live after all, with
	// core.FailureInvalidInput for a proof that is none, and with the context's error when ctx is
	// done.
	Forget(ctx context.Context, ref lifecycle.Ref, proof lifecycle.DeathProof) error
}
