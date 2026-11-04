package gitrepo

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Fake is Git and Resolver over repositories described in memory: repositories with their
// revisions, and linked worktrees of them. It is a real implementation that tests of the command
// line and of the daemon use, so none of them depends on the developer's repositories. It is as
// strict as git: a directory in no repository it was told of is in no repository.
type Fake struct {
	mu    sync.Mutex
	repos map[string]map[string]core.Commit
	// worktrees maps a linked worktree's root to its repository's.
	worktrees map[string]string
	// own maps a linked worktree's root to the revisions it reads differently from its repository,
	// such as HEAD and what is relative to it.
	own map[string]map[string]core.Commit
}

var _ Revisions = (*Fake)(nil)

// NewFake returns a Fake with no repository.
func NewFake() *Fake {
	return &Fake{repos: map[string]map[string]core.Commit{}, worktrees: map[string]string{}, own: map[string]map[string]core.Commit{}}
}

// AddRepo makes a repository at the absolute path, whose revisions are the given names for
// commits. A full commit always names itself.
func (f *Fake) AddRepo(path string, revisions map[string]core.Commit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.repos[filepath.Clean(path)] = revisions
}

// AddWorktree makes path a linked worktree of the repository at repo, which must have been added.
// It reads the repository's revisions, as a linked worktree shares its branches and commits, except
// those in own, which it reads as its own, as git reads HEAD and HEAD~1 in each worktree. own may
// be nil.
func (f *Fake) AddWorktree(path, repo string, own map[string]core.Commit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.worktrees[filepath.Clean(path)] = filepath.Clean(repo)
	f.own[filepath.Clean(path)] = own
}

// repoOf returns the repository that contains dir, through a worktree when dir is in one, and the
// root of the work tree that contains dir.
func (f *Fake) repoOf(dir string) (repo, workTree string, ok bool) {
	for d := filepath.Clean(dir); ; d = filepath.Dir(d) {
		if _, ok := f.repos[d]; ok {
			return d, d, true
		}
		if repo, ok := f.worktrees[d]; ok {
			_, known := f.repos[repo]
			return repo, d, known
		}
		if d == filepath.Dir(d) {
			return "", "", false
		}
	}
}

// Resolve implements Resolver: the repository whose root, or whose linked worktree's root, is dir
// or a parent of it.
func (f *Fake) Resolve(_ context.Context, dir string) (core.Repository, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	if filepath.IsAbs(dir) {
		if repo, _, ok := f.repoOf(dir); ok {
			return core.Repository(repo), nil
		}
	}
	return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("context.repository: %s is not in a git repository", dir)}
}

// Commit implements Git.
func (f *Fake) Commit(_ context.Context, dir, revision string) (core.Commit, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	repository, workTree, ok := f.repoOf(dir)
	if !ok {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s is not in a repository", dir)}
	}
	if c, ok := f.own[workTree][revision]; ok {
		return c, nil
	}
	revs := f.repos[repository]
	if c, ok := revs[revision]; ok {
		return c, nil
	}
	if c := core.Commit(revision); c.Valid() {
		return c, nil
	}
	// An abbreviated hash names the one commit it starts.
	var found core.Commit
	for _, c := range revs {
		if len(revision) >= 4 && strings.HasPrefix(string(c), revision) {
			if found != "" && found != c {
				return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("commit: %q is ambiguous", revision)}
			}
			found = c
		}
	}
	if found == "" {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("commit: %q is not a commit of %s", revision, repository)}
	}
	return found, nil
}
