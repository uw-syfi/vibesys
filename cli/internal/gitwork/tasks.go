package gitwork

import (
	"archive/tar"
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"maps"
	"path"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// MaxTaskBytes bounds the files of one task directory that Tasks reads. A task is scripts and
// small data: a directory over the limit is refused, not read in part, since its version is a
// digest of all of it.
const MaxTaskBytes = 64 << 20

// MaxCandidateBytes bounds what Files reads at once: a candidate's Dockerfile and build context,
// which are read into memory to digest and send to the image builder.
const MaxCandidateBytes = 1 << 30

// Tasks is a task.Source over the git program, and the task.Files of what builds a candidate's
// image.
type Tasks struct{}

var (
	_ task.Source = Tasks{}
	_ task.Files  = Tasks{}
)

// Files implements task.Files with git archive of the paths at the commit.
func (Tasks) Files(ctx context.Context, repository string, commit core.Commit, paths []string) (task.Tree, error) {
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	ok, err := hasCommit(ctx, repository, commit)
	if err != nil {
		return nil, err
	}
	if !ok {
		return nil, noCommit(repository, commit)
	}
	tree := task.Tree{}
	for _, p := range paths {
		// A path the commit lacks makes git archive fail, and is the same as an empty one.
		data, err := git(ctx, repository, "archive", "--format=tar", string(commit), "--", p)
		if err != nil {
			if ctx.Err() != nil {
				return nil, ctx.Err()
			}
			continue
		}
		got, err := readTar(data, MaxCandidateBytes, "the candidate's files")
		if err != nil {
			return nil, err
		}
		maps.Copy(tree, got)
	}
	return tree, nil
}

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
	tree, err := readTar(data, MaxTaskBytes, "the task's files")
	if err != nil {
		return task.Task{}, err
	}
	return task.Read(tree, name)
}

// readTar reads the regular files and symbolic links of a tar archive, of at most limit bytes in
// all, which is what git archive writes besides directories and its header of the commit. Any
// other entry is refused, not left out, so a task never silently loses a file.
func readTar(archive []byte, limit int64, what string) (task.Tree, error) {
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
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading %s: %v", what, err)}
		}
		switch h.Typeflag {
		case tar.TypeDir, tar.TypeXGlobalHeader:
			continue
		case tar.TypeSymlink:
			tree[path.Clean(h.Name)] = task.File{Link: h.Linkname}
			continue
		case tar.TypeReg:
		default:
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading %s: %s is an entry of type %q, which a task cannot hold", what, h.Name, h.Typeflag)}
		}
		if total += h.Size; total > limit {
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("%s are over %d bytes, more than this build reads", what, limit)}
		}
		data, err := io.ReadAll(r)
		if err != nil {
			return nil, &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf("reading %s: %v", what, err)}
		}
		tree[path.Clean(h.Name)] = task.File{Data: data, Executable: h.Mode&0o111 != 0}
	}
}
