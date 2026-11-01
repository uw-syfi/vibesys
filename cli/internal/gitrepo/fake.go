package gitrepo

import (
	"context"
	"fmt"
	"path/filepath"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Fake is Git over repositories described in memory. It is a real implementation of Git that
// tests of the command line, which must not depend on the developer's repositories, use.
type Fake struct {
	mu    sync.Mutex
	repos map[string]map[string]core.Commit
}

var _ Git = (*Fake)(nil)

// NewFake returns a Fake with no repository.
func NewFake() *Fake { return &Fake{repos: map[string]map[string]core.Commit{}} }

// AddRepo makes a repository at the absolute path, whose revisions are the given names for
// commits. A full commit always names itself.
func (f *Fake) AddRepo(path string, revisions map[string]core.Commit) {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.repos[path] = revisions
}

// Toplevel implements Git: the repository whose path is dir or a parent of it.
func (f *Fake) Toplevel(_ context.Context, dir string) (string, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	for d := filepath.Clean(dir); ; d = filepath.Dir(d) {
		if _, ok := f.repos[d]; ok {
			return d, nil
		}
		if d == filepath.Dir(d) {
			break
		}
	}
	return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("%s is not in a git repository", dir)}
}

// Commit implements Git.
func (f *Fake) Commit(_ context.Context, repository, revision string) (core.Commit, error) {
	f.mu.Lock()
	defer f.mu.Unlock()
	revs, ok := f.repos[repository]
	if !ok {
		return "", &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s is not a repository", repository)}
	}
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
