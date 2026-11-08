package slurm

import (
	"context"
	"fmt"
	"strings"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/remote"
)

// CallTimeout bounds one call of a Slurm command.
const CallTimeout = 2 * time.Minute

// ConfirmTimeout bounds the wait for a cancelled job to end.
const ConfirmTimeout = 10 * time.Minute

// CLI is the Scheduler over Slurm's commands, run through a shell on a host of the cluster.
type CLI struct {
	Shell  remote.Shell
	Timers clock.Timers
	// Poll is the pause between two reads of a job's state.
	Poll time.Duration
}

var _ Scheduler = CLI{}

// user is how a command names the shell's user: Slurm's commands are asked about that user's jobs
// only.
const user = `"$(id -un)"`

// run runs the script on the host, each attempt bounded by CallTimeout: a call that does not
// reach the host is made again as remote.Reconnect says.
func (c CLI) run(ctx context.Context, script string, args ...string) (string, error) {
	return remote.Within(ctx, c.Timers, c.Shell, CallTimeout, append([]string{"sh", "-c", script, "vs-slurm"}, args...), "")
}

// Jobs implements Scheduler.
func (c CLI) Jobs(ctx context.Context, scope string) ([]string, error) {
	out, err := c.run(ctx, `squeue -h -u `+user+` -o '%j|%k'`)
	if err != nil {
		return nil, fmt.Errorf("listing the Slurm jobs: %w", err)
	}
	return ParseJobs(out, scope), nil
}

// ParseJobs reads squeue's "name|comment" lines and returns the names of this package's jobs whose
// comment is the scope.
func ParseJobs(out, scope string) []string {
	var names []string
	for _, line := range strings.Split(out, "\n") {
		name, comment, ok := strings.Cut(strings.TrimSpace(line), "|")
		if ok && strings.HasPrefix(name, JobPrefix) && strings.TrimSpace(comment) == scope {
			names = append(names, name)
		}
	}
	return names
}

// live are the states of a job that still holds, or waits for, its allocation.
var live = map[string]bool{
	"PENDING": true, "RUNNING": true, "SUSPENDED": true, "COMPLETING": true, "CONFIGURING": true,
	"REQUEUED": true, "RESIZING": true, "STOPPED": true, "SIGNALING": true, "STAGE_OUT": true,
}

// Ended reports whether squeue's and sacct's states of a job say it no longer runs: squeue lists
// none, and sacct none that is live (sacct may lag behind squeue, and is asked second).
func Ended(squeue, sacct string) bool {
	if strings.TrimSpace(squeue) != "" {
		return false
	}
	for _, state := range strings.Fields(sacct) {
		// sacct writes "CANCELLED by 1002"; the first word is the state.
		if live[strings.ToUpper(strings.TrimRight(state, "+"))] {
			return false
		}
	}
	return true
}

// Cancel implements Scheduler.
func (c CLI) Cancel(ctx context.Context, name string) error {
	if _, err := c.run(ctx, `scancel -u `+user+` -n "$1"`, name); err != nil {
		return fmt.Errorf("cancelling the Slurm job %s: %w", name, err)
	}
	limit, stop := c.Timers.Timeout(ctx, ConfirmTimeout)
	defer stop()
	for {
		squeue, err := c.run(ctx, `squeue -h -u `+user+` -n "$1" -o %T`, name)
		if err != nil {
			return fmt.Errorf("reading the state of the Slurm job %s: %w", name, err)
		}
		var sacct string
		if strings.TrimSpace(squeue) == "" {
			// sacct may not be configured; then squeue alone says it.
			sacct, _ = c.run(ctx, `sacct -n -X -P -u `+user+` --name="$1" -S now-2days -o State 2>/dev/null || true`, name)
		}
		if Ended(squeue, sacct) {
			return nil
		}
		pause, stopPause := c.Timers.Timeout(ctx, c.Poll)
		select {
		case <-pause:
			stopPause()
		case <-limit:
			stopPause()
			return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the Slurm job %s was cancelled and still runs after %v", name, ConfirmTimeout)}
		case <-ctx.Done():
			stopPause()
			return ctx.Err()
		}
	}
}
