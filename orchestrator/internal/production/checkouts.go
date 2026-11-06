package production

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/core"
	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// checkoutDir is where an agent's working copy of the repository is: a detached git worktree in the
// agent's directory, made when its first session launched. The lead works in the repository itself
// and has none.
func (s *ProcessSidecars) checkoutDir(agent core.AgentID) string {
	return filepath.Join(s.agentDir(agent), "worktree")
}

// CommitRefPrefix is where the commits of removed working copies are kept, as refs/vibesys/runs/<run>/<agent>.
// A copy is a detached worktree, so a commit made in it is on no branch, and git gc would prune it once
// the copy is gone. The namespace is one no branch listing or checkout shows.
const CommitRefPrefix = "refs/vibesys/runs/"

// Release implements daemon.Checkouts. A copy whose HEAD no branch or kept ref holds is first kept
// under CommitRefPrefix; if that ref cannot be written the copy stays, since removing it would leave
// the commit to be pruned. Then git removes the worktree, and refuses one with modified or untracked
// files, which is exactly the work that would be lost; that copy is kept as well, and so is one that
// could not be removed for any other reason, since it is not known to be clean. A copy whose agent made
// no commit has a HEAD a branch already holds and gets no ref.
func (s *ProcessSidecars) Release(ctx context.Context, repository string, run core.RunID, agents []core.AgentID) ([]core.Checkout, error) {
	var kept []core.Checkout
	for _, agent := range agents {
		path := s.checkoutDir(agent)
		if _, err := os.Stat(path); err != nil {
			continue // never made, or already removed.
		}
		keep := core.Checkout{Agent: agent, Location: path}
		if err := keepCommit(ctx, repository, path, string(run), string(agent)); err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			kept = append(kept, keep)
			continue
		}
		if err := exec.CommandContext(ctx, "git", "-C", repository, "worktree", "remove", path).Run(); err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			if _, still := os.Stat(path); still == nil {
				kept = append(kept, keep)
			}
		}
	}
	// A worktree whose directory is gone leaves a record in the repository; this drops those.
	_ = exec.CommandContext(ctx, "git", "-C", repository, "worktree", "prune").Run()
	return kept, ctx.Err()
}

// keepCommit writes the ref that holds the HEAD of the working copy at path, unless a branch, tag or
// kept ref already reaches it. It fails when the commit cannot be read or the ref cannot be written.
func keepCommit(ctx context.Context, repository, path, run, agent string) error {
	out, err := exec.CommandContext(ctx, "git", "-C", path, "rev-parse", "--verify", "HEAD").Output()
	if err != nil {
		return fmt.Errorf("read the head of %s: %w", path, err)
	}
	head := strings.TrimSpace(string(out))
	held, err := exec.CommandContext(ctx, "git", "-C", repository, "for-each-ref", "--contains", head, "--count=1",
		"--format=%(refname)", "refs/heads", "refs/remotes", "refs/tags", CommitRefPrefix).Output()
	if err != nil {
		return fmt.Errorf("find the refs holding %s: %w", head, err)
	}
	if strings.TrimSpace(string(held)) != "" {
		return nil
	}
	ref := CommitRefPrefix + run + "/" + agent
	if err := exec.CommandContext(ctx, "git", "-C", repository, "update-ref", ref, head).Run(); err != nil {
		return fmt.Errorf("write %s: %w", ref, err)
	}
	return nil
}

// checkoutsOf is the launcher's way to remove working copies, or nil for a launcher that makes none.
func checkoutsOf(sidecars daemon.Sidecars) daemon.Checkouts {
	if c, ok := sidecars.(daemon.Checkouts); ok {
		return c
	}
	return nil
}

var _ daemon.Checkouts = (*ProcessSidecars)(nil)
