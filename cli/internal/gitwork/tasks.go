package gitwork

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"path"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// MaxTaskBytes bounds the files of one task directory that Tasks reads. A task is scripts and
// small data: a directory over the limit is refused, not read in part, since its version is a
// digest of all of it.
const MaxTaskBytes = 64 << 20

// Tasks is a task.Source over the git program.
type Tasks struct{}

var _ task.Source = Tasks{}

// Task implements task.Source. It reads the task directory at the commit with git archive, and
// reads the task from those files exactly as a source of any kind does.
func (Tasks) Task(ctx context.Context, repository string, commit core.Commit, name string) (task.Task, error) {
	if err := task.ValidName(name); err != nil {
		return task.Task{}, err
	}
	if err := ctx.Err(); err != nil {
		return task.Task{}, err
	}
	ok, err := hasCommit(ctx, repository, commit)
	if err != nil {
		return task.Task{}, err
	}
	if !ok {
		return task.Task{}, noCommit(repository, commit)
	}
	dir := path.Join(task.TasksDir, name)
	// A directory the commit lacks makes git archive fail, and is the same as an empty one: Read
	// says the task is not there.
	data, err := git(ctx, repository, "archive", "--format=tar", string(commit), "--", dir)
	if err != nil {
		if ctx.Err() != nil {
			return task.Task{}, ctx.Err()
		}
		data = nil
	}
	tree, err := treeOf(data)
	if err != nil {
		return task.Task{}, err
	}
	return task.Read(tree, name)
}

// treeOf reads the regular files of a tar archive. Links and special files are left out, as
// task.Version leaves them out.
func treeOf(archive []byte) (task.Tree, error) {
	tree := task.Tree{}
	if len(archive) == 0 {
		return tree, nil
	}
	var total int64
	r := tar.NewReader(bytes.NewReader(archive))
	for {
		h, err := r.Next()
		if errors.Is(err, io.EOF) {
			return tree, nil
		}
		if err != nil {
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading the task's files: %v", err)}
		}
		if h.Typeflag != tar.TypeReg {
			continue
		}
		if total += h.Size; total > MaxTaskBytes {
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("the task's files are over %d bytes, which is more than a task is", MaxTaskBytes)}
		}
		data, err := io.ReadAll(r)
		if err != nil {
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading the task's files: %v", err)}
		}
		tree[path.Clean(h.Name)] = task.File{Data: data, Executable: h.Mode&0o111 != 0}
	}
}
