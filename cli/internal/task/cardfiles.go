package task

import (
	"context"
	"fmt"
	"path"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// CardFileNames are the files of a task a card is read from, by name in the task's directory.
var CardFileNames = []string{ManifestName, ObjectiveName, DescriptionName}

// CardFiles reaches the files a card is read from, where a task is. Each method returns a Tree with
// the files of CardFileNames that the task has, under their paths from the repository's root
// (.vibesys/tasks/<name>/...), and never anything else: a task with a large directory costs no more
// than its three files. A file that is not a regular file is an error, not a read through a link.
// Every method fails with core.FailureNotFound for a repository, revision or folder that is not there;
// a task that has none of the files returns an empty Tree, which Card reports as not found.
type CardFiles interface {
	// Committed reads the task as the commit has it. An empty revision is HEAD.
	Committed(ctx context.Context, repository, revision, name string) (Tree, error)
	// WorkingTree reads the task as it stands in the repository's working tree.
	WorkingTree(ctx context.Context, repository, name string) (Tree, error)
	// Folder reads the task of a folder that need not be a repository.
	Folder(ctx context.Context, root, name string) (Tree, error)
	// WorkingTask reads every file of the task's directory as it stands in the repository's working
	// tree (links as links, nothing generated), which is what the task's version names. It is the
	// one method that reads more than the card's files, and DraftOf is what reads its tree. It
	// fails as WorkingTree does, and for a file that is neither a regular file nor a link with
	// core.FailureInvalidInput.
	WorkingTask(ctx context.Context, repository, name string) (Tree, error)
}

// MemoryCardFiles is a CardFiles over trees described in memory. It is safe for concurrent use.
type MemoryCardFiles struct {
	mu        sync.Mutex
	committed map[[2]string]Tree
	working   map[string]Tree
	folders   map[string]Tree
}

var _ CardFiles = (*MemoryCardFiles)(nil)

// NewMemoryCardFiles returns a CardFiles with no repository and no folder.
func NewMemoryCardFiles() *MemoryCardFiles {
	return &MemoryCardFiles{committed: map[[2]string]Tree{}, working: map[string]Tree{}, folders: map[string]Tree{}}
}

// PutCommit sets the files of a revision of a repository; "HEAD" is the default revision.
func (m *MemoryCardFiles) PutCommit(repository, revision string, files Tree) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.committed[[2]string{repository, revision}] = files
}

// PutWorkingTree sets the files of a repository's working tree.
func (m *MemoryCardFiles) PutWorkingTree(repository string, files Tree) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.working[repository] = files
}

// PutFolder sets the files of a folder.
func (m *MemoryCardFiles) PutFolder(root string, files Tree) {
	m.mu.Lock()
	defer m.mu.Unlock()
	m.folders[root] = files
}

// Committed implements CardFiles.
func (m *MemoryCardFiles) Committed(ctx context.Context, repository, revision, name string) (Tree, error) {
	if revision == "" {
		revision = "HEAD"
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	return cardOnly(ctx, m.committed[[2]string{repository, revision}], name, fmt.Sprintf("revision %q of %s", revision, repository))
}

// WorkingTree implements CardFiles.
func (m *MemoryCardFiles) WorkingTree(ctx context.Context, repository, name string) (Tree, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return cardOnly(ctx, m.working[repository], name, "the working tree of "+repository)
}

// WorkingTask implements CardFiles.
func (m *MemoryCardFiles) WorkingTask(ctx context.Context, repository, name string) (Tree, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := ValidName(name); err != nil {
		return nil, err
	}
	m.mu.Lock()
	defer m.mu.Unlock()
	tree, ok := m.working[repository]
	if !ok {
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: "the working tree of " + repository + " does not exist"}
	}
	return tree.Under([]string{path.Join(TasksDir, name)}), nil
}

// Folder implements CardFiles.
func (m *MemoryCardFiles) Folder(ctx context.Context, root, name string) (Tree, error) {
	m.mu.Lock()
	defer m.mu.Unlock()
	return cardOnly(ctx, m.folders[root], name, "folder "+root)
}

func cardOnly(ctx context.Context, tree Tree, name, where string) (Tree, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if err := ValidName(name); err != nil {
		return nil, err
	}
	if tree == nil {
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: where + " does not exist"}
	}
	out := Tree{}
	for _, f := range CardFileNames {
		p := path.Join(TasksDir, name, f)
		if file, ok := tree[p]; ok {
			if file.IsLink() {
				return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: p + " is a symbolic link, not a file"}
			}
			out[p] = file
		}
	}
	return out, nil
}
