package production

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// checkoutDir is where an agent's working copy of the repository is: a detached git worktree in the
// agent's directory, made when its first session launched. The lead works in the repository itself
// and has none.
func (s *ProcessSidecars) checkoutDir(agent core.AgentID) string {
	return filepath.Join(s.agentDir(agent), "worktree")
}

// Release implements daemon.Checkouts. It asks git to remove each worktree and keeps the ones git
// refuses to: git refuses a worktree with modified or untracked files, which is exactly the work that
// would be lost, and a commit made there is in the repository's object store either way. A worktree
// that could not be removed for any other reason is kept as well, since it is not known to be clean.
func (s *ProcessSidecars) Release(ctx context.Context, repository string, agents []core.AgentID) ([]core.Checkout, error) {
	var kept []core.Checkout
	for _, agent := range agents {
		path := s.checkoutDir(agent)
		if _, err := os.Stat(path); err != nil {
			continue // never made, or already removed.
		}
		if err := exec.CommandContext(ctx, "git", "-C", repository, "worktree", "remove", path).Run(); err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if _, still := os.Stat(path); still == nil {
				kept = append(kept, core.Checkout{Agent: agent, Location: path})
			}
		}
	}
	// A worktree whose directory is gone leaves a record in the repository; this drops those.
	_ = exec.CommandContext(ctx, "git", "-C", repository, "worktree", "prune").Run()
	return kept, ctx.Err()
}

// checkoutsOf is the launcher's way to remove working copies, or nil for a launcher that makes none.
func checkoutsOf(sidecars daemon.Sidecars) daemon.Checkouts {
	if c, ok := sidecars.(daemon.Checkouts); ok {
		return c
	}
	return nil
}

var _ daemon.Checkouts = (*ProcessSidecars)(nil)
