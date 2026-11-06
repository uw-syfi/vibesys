package daemon

import (
	"context"
	"errors"
	"fmt"
	"time"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
)

// The versions this build speaks. ProtocolVersion rises only when a change breaks a peer.
const (
	ProtocolVersion    uint32 = 1
	MinProtocolVersion uint32 = 1
)

// MergeWindow is how often the daemon ends the hub's merge window. Run ticks at this rate.
const MergeWindow = 50 * time.Millisecond

// Clock tells the time. Production reads the system clock; a test sets one.
type Clock interface{ Now() time.Time }

// Nonces mints the nonce of each input. Two calls never return the same value, across restarts.
type Nonces interface{ Next() core.Nonce }

// Bounds gives each call the daemon makes outside itself a limit, so that one call that never
// returns cannot stall the loop that serves every agent. Production returns a context with a
// timeout; a test returns one it ends itself. The daemon asks for a bound for every call to a
// Sidecars, Sidecar, JobSource or PlanReader, and treats the end of the bound as a failure to retry:
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

// ErrIdle is what an EventStream returns when it has nothing ready. The caller asks again on a
// later step. A real stream, which blocks in a goroutine of its own, hands the daemon what it has
// buffered and returns ErrIdle when the buffer is empty, so the daemon never waits on one.
var ErrIdle = errors.New("daemon: no sidecar event is ready")

// LaunchEnv is what the daemon gives a session's process at launch, so that the agent's tools reach
// the daemon as that agent. It names the agent-only socket and never the operator's. The sidecar item decides how it reaches the process.
type LaunchEnv map[string]string

// The names in a LaunchEnv. The agent's identity is here and nowhere in a tool's arguments.
const (
	EnvSocket  = "VIBESYS_ORCHESTRATOR_SOCKET"
	EnvAgent   = "VIBESYS_ORCHESTRATOR_AGENT"
	EnvSession = "VIBESYS_ORCHESTRATOR_SESSION"
	EnvToken   = "VIBESYS_ORCHESTRATOR_TOKEN"
	EnvRun     = "VIBESYS_ORCHESTRATOR_RUN"
	EnvRole    = "VIBESYS_ORCHESTRATOR_ROLE"
)

// Sidecars starts and finds the sidecars. A sidecar serves the contract in sidecar.proto.
type Sidecars interface {
	// Launch starts the sidecar of a session, or returns the one already running for it, so a
	// launch delivered twice starts one. ErrSidecarGone is not an error of Launch: any error means
	// the launch failed.
	Launch(ctx context.Context, launch core.LaunchSession, env LaunchEnv) (Sidecar, error)
	// Attach finds the sidecar of a session that was launched before this daemon started. It
	// returns ErrSidecarGone when there is none.
	Attach(ctx context.Context, session core.Session) (Sidecar, error)
}

// Sidecar is the daemon's side of one session's SidecarService.
type Sidecar interface {
	// Send carries out a command. A repeated effect id answers as the first delivery did. It
	// returns ErrSidecarGone when the sidecar is no longer there.
	Send(ctx context.Context, cmd *pb.SidecarCommand) error
	// Follow streams the session's events after an offset. Its context bounds the call that opens the
	// stream and not the stream, which outlives it and ends with Close.
	Follow(ctx context.Context, afterOffset uint64) (EventStream, error)
	// Kill ends the process without waiting for it. It is for a session written off.
	Kill(ctx context.Context) error
}

// EventStream is one Follow. Next returns ErrIdle when nothing is ready, ErrSidecarGone when the
// sidecar ended, and any other error when the stream broke and may be reopened.
type EventStream interface {
	Next() (*pb.SidecarEvent, error)
	Close()
}

// PlanReader reads plans through vibesys-cli. The orchestrator shows them and decides nothing on
// them. ErrPlanNotFound says the number names no plan.
type PlanReader interface {
	ListPlans(ctx context.Context, run core.Run) ([]*pb.Plan, error)
	ViewPlan(ctx context.Context, run core.Run, number uint32) (*pb.Plan, error)
}

// ErrPlanNotFound is the answer to a plan number that names no plan.
var ErrPlanNotFound = errors.New("daemon: no such plan")

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
}

// StartRefusal says why a start was refused, in words that name the offending path or setting.
type StartRefusal struct {
	Reason string
	// NotReady says the daemon, and not the request, is at fault: no agent can launch whatever the
	// repository holds. Otherwise the repository or the task is wrong.
	NotReady bool
}

func (r *StartRefusal) Error() string { return r.Reason }
