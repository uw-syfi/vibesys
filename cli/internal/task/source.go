package task

import (
	"context"
	"fmt"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Source reads a task as a commit of a repository has it. The daemon asks for it when a job is
// submitted, to key the job, and again when the job starts, to plan it, so the task a job runs is
// the task of its commit and never of whatever is checked out now.
type Source interface {
	// Task returns the task called name at the commit. It fails with core.FailureNotFound for a
	// commit or a task the repository does not have, and otherwise as Read does for a task it
	// cannot run.
	Task(ctx context.Context, repository string, commit core.Commit, name string) (Task, error)
}

// MemorySource is a Source over repositories described in memory. It reads a task through Read,
// like any source, so it differs from git only in where the files come from. It is safe for
// concurrent use.
type MemorySource struct {
	mu    sync.Mutex
	trees map[treeAt]Tree
}

type treeAt struct {
	repository string
	commit     core.Commit
}

var _ Source = (*MemorySource)(nil)

// NewMemorySource returns a source with no repository.
func NewMemorySource() *MemorySource { return &MemorySource{trees: map[treeAt]Tree{}} }

// Put sets the files of a commit of a repository.
func (m *MemorySource) Put(repository string, commit core.Commit, files Tree) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.trees[treeAt{repository, commit}] = files
}

// Task implements Source.
func (m *MemorySource) Task(ctx context.Context, repository string, commit core.Commit, name string) (Task, error) {
	if err := ctx.Err(); err != nil {
		return Task{}, err
	}
	m.mu.Lock()
	tree, ok := m.trees[treeAt{repository, commit}]
	m.mu.Unlock()
	if !ok {
		return Task{}, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("commit %s is not a commit of %s", commit, repository)}
	}
	return Read(tree, name)
}
