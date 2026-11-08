package gitwork

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"strconv"
	"strings"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// MaxCardFileBytes bounds each file a card is read from. They are a manifest, a list of objectives and
// a description; a larger one is refused, not read in part.
const MaxCardFileBytes = 1 << 20

// CardFiles is a task.CardFiles over git, for a committed task, and over the file system, for a
// working tree or a folder. It reads only the files of task.CardFileNames.
type CardFiles struct{}

var _ task.CardFiles = CardFiles{}

// Committed implements task.CardFiles: the three files as the commit holds them, found with git
// ls-tree and read as blobs, so a symbolic link in the task is refused and never followed.
func (CardFiles) Committed(ctx context.Context, repository, revision, name string) (task.Tree, error) {
	if err := task.ValidName(name); err != nil {
		return nil, err
	}
	if revision == "" {
		revision = "HEAD"
	}
	if strings.HasPrefix(revision, "-") || strings.ContainsAny(revision, "\x00\n") {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("revision: %q is not a revision", revision)}
	}
	if _, err := git(ctx, repository, "rev-parse", "--git-dir"); err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s is not a repository git can read", repository)}
	}
	out, err := git(ctx, repository, "rev-parse", "--verify", "--quiet", "--end-of-options", revision+"^{commit}")
	if err != nil {
		if ctx.Err() != nil {
			return nil, ctx.Err()
		}
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("revision %q is not a commit of %s", revision, repository)}
	}
	commit := strings.TrimSpace(string(out))
	dir := path.Join(task.TasksDir, name)
	paths := make([]string, 0, len(task.CardFileNames))
	for _, f := range task.CardFileNames {
		paths = append(paths, path.Join(dir, f))
	}
	listing, err := git(ctx, repository, append([]string{"ls-tree", "-z", commit, "--"}, paths...)...)
	if err != nil {
		return nil, unavailable("listing the task %q at %s: %v", name, revision, err)
	}
	tree := task.Tree{}
	for _, entry := range strings.Split(string(listing), "\x00") {
		if entry == "" {
			continue
		}
		meta, p, ok := strings.Cut(entry, "\t")
		fields := strings.Fields(meta)
		if !ok || len(fields) != 3 {
			return nil, unavailable("git ls-tree answered %q", entry)
		}
		mode, kind, sha := fields[0], fields[1], fields[2]
		if kind != "blob" || (mode != "100644" && mode != "100755") {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: p + " is not a regular file"}
		}
		size, err := git(ctx, repository, "cat-file", "-s", sha)
		if err != nil {
			return nil, unavailable("reading %s: %v", p, err)
		}
		if n, _ := strconv.ParseInt(strings.TrimSpace(string(size)), 10, 64); n > MaxCardFileBytes {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("%s is over %d bytes", p, MaxCardFileBytes)}
		}
		data, err := git(ctx, repository, "cat-file", "blob", sha)
		if err != nil {
			return nil, unavailable("reading %s: %v", p, err)
		}
		tree[p] = task.File{Data: data, Executable: mode == "100755"}
	}
	return tree, nil
}

// WorkingTree implements task.CardFiles.
func (c CardFiles) WorkingTree(ctx context.Context, repository, name string) (task.Tree, error) {
	return readCardFiles(ctx, repository, name)
}

// maxWorkingTaskBytes and maxWorkingTaskFiles bound what WorkingTask reads of a task directory: a
// task is scripts and a few data files, and a directory over these is not one the daemon takes into
// memory to version.
const (
	maxWorkingTaskBytes = 256 << 20
	maxWorkingTaskFiles = 20000
)

// WorkingTask implements task.CardFiles: every file under the task's directory, a link as a link,
// without the files a tool generates (task.Generated).
func (CardFiles) WorkingTask(ctx context.Context, repository, name string) (task.Tree, error) {
	if err := task.ValidName(name); err != nil {
		return nil, err
	}
	if info, err := os.Stat(repository); err != nil || !info.IsDir() {
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s is not a directory", repository)}
	}
	dir := path.Join(task.TasksDir, name)
	base := filepath.Join(repository, filepath.FromSlash(dir))
	tree := task.Tree{}
	var total int64
	err := filepath.WalkDir(base, func(full string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if cerr := ctx.Err(); cerr != nil {
			return cerr
		}
		relOS, err := filepath.Rel(base, full)
		if err != nil || relOS == "." {
			return err
		}
		rel := filepath.ToSlash(relOS)
		if d.IsDir() {
			if task.Generated(rel) {
				return fs.SkipDir
			}
			return nil
		}
		if task.Generated(rel) {
			return nil
		}
		p := path.Join(dir, rel)
		info, err := d.Info()
		if err != nil {
			return err
		}
		switch {
		case info.Mode()&fs.ModeSymlink != 0:
			target, err := os.Readlink(full)
			if err != nil {
				return err
			}
			tree[p] = task.File{Link: target}
		case info.Mode().IsRegular():
			total += info.Size()
			if total > maxWorkingTaskBytes || len(tree) >= maxWorkingTaskFiles {
				return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("%s is over %d files or %d bytes", dir, maxWorkingTaskFiles, maxWorkingTaskBytes)}
			}
			data, err := os.ReadFile(full)
			if err != nil {
				return err
			}
			tree[p] = task.File{Data: data, Executable: info.Mode()&0o111 != 0}
		default:
			return &core.Failure{Kind: core.FailureInvalidInput, Message: p + " is not a regular file"}
		}
		return nil
	})
	var f *core.Failure
	switch {
	case err == nil:
		return tree, nil
	case errors.As(err, &f):
		return nil, f
	case errors.Is(err, fs.ErrNotExist) && len(tree) == 0:
		return tree, nil
	case ctx.Err() != nil:
		return nil, ctx.Err()
	}
	return nil, unavailable("reading the task %q in %s: %v", name, repository, err)
}

// Folder implements task.CardFiles.
func (c CardFiles) Folder(ctx context.Context, root, name string) (task.Tree, error) {
	return readCardFiles(ctx, root, name)
}

// readCardFiles reads the files of a task under a directory, refusing a file that is not a regular
// file (a link is not followed, so a task cannot make the daemon read outside it).
func readCardFiles(ctx context.Context, root, name string) (task.Tree, error) {
	if err := task.ValidName(name); err != nil {
		return nil, err
	}
	if err := ctx.Err(); err != nil {
		return nil, err
	}
	if info, err := os.Stat(root); err != nil || !info.IsDir() {
		return nil, &core.Failure{Kind: core.FailureNotFound, Message: fmt.Sprintf("%s is not a directory", root)}
	}
	tree := task.Tree{}
	for _, f := range task.CardFileNames {
		rel := path.Join(task.TasksDir, name, f)
		full := filepath.Join(root, filepath.FromSlash(rel))
		info, err := os.Lstat(full)
		if os.IsNotExist(err) {
			continue
		}
		if err != nil {
			return nil, unavailable("reading %s: %v", rel, err)
		}
		if !info.Mode().IsRegular() {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: rel + " is not a regular file"}
		}
		if info.Size() > MaxCardFileBytes {
			return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("%s is over %d bytes", rel, MaxCardFileBytes)}
		}
		data, err := os.ReadFile(full)
		if err != nil {
			return nil, unavailable("reading %s: %v", rel, err)
		}
		tree[rel] = task.File{Data: data, Executable: info.Mode()&0o111 != 0}
	}
	return tree, nil
}

func unavailable(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(format, args...)}
}
