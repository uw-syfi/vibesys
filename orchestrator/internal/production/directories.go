package production

import (
	"context"
	"errors"
	"fmt"
	"io/fs"
	"path/filepath"
	"slices"
	"strings"

	"github.com/uw-syfi/vibesys/orchestrator/internal/daemon"
)

// MaxDirectoryEntries caps one listing, so a folder of a hundred thousand files costs a few hundred
// stats and a small answer.
const MaxDirectoryEntries = 300

// DirectoryLister lists a folder of the host's file system for a path being typed. It is a
// daemon.Directories.
type DirectoryLister struct{ Tree Tree }

// List implements daemon.Directories. Only the folder's own children are looked at: for a child that
// is a directory, whether it holds a .git and a task, and nothing else below it.
func (l DirectoryLister) List(ctx context.Context, path string, hidden bool) (daemon.Listing, error) {
	dir, err := l.resolve(path)
	if err != nil {
		return daemon.Listing{}, err
	}
	fault := func(kind daemon.DirectoryFault, format string, args ...any) (daemon.Listing, error) {
		return daemon.Listing{}, &daemon.DirectoryError{Fault: kind, Reason: fmt.Sprintf(format, args...)}
	}
	if !l.Tree.Exists(dir) {
		return fault(daemon.DirectoryNotFound, "path %s does not exist", dir)
	}
	if !l.Tree.IsDir(dir) {
		return fault(daemon.DirectoryNotADirectory, "path %s is not a directory", dir)
	}
	names, err := l.Tree.ReadDirNames(dir)
	switch {
	case errors.Is(err, fs.ErrPermission):
		return fault(daemon.DirectoryDenied, "path %s cannot be read: permission denied", dir)
	case errors.Is(err, fs.ErrNotExist):
		return fault(daemon.DirectoryNotFound, "path %s does not exist", dir)
	case err != nil:
		return daemon.Listing{}, fmt.Errorf("list %s: %w", dir, err)
	}
	names = slices.DeleteFunc(names, func(n string) bool { return !hidden && strings.HasPrefix(n, ".") })
	slices.Sort(names)
	listing := daemon.Listing{}
	if len(names) > MaxDirectoryEntries {
		names, listing.Truncated = names[:MaxDirectoryEntries], true
	}
	for _, name := range names {
		if ctx.Err() != nil {
			// The time bound ended: what was read so far is the answer.
			listing.Truncated = true
			break
		}
		child := filepath.Join(dir, name)
		if !l.Tree.Exists(child) { // A broken link, or an entry that went away since it was read.
			continue
		}
		entry := daemon.DirectoryEntry{Name: name, IsDirectory: l.Tree.IsDir(child)}
		if entry.IsDirectory {
			entry.IsGitRepository = l.Tree.Exists(filepath.Join(child, ".git"))
			tasks, _ := taskNames(l.Tree, child) // A folder whose tasks cannot be read shows none.
			entry.HasTasks = len(tasks) > 0
		}
		listing.Entries = append(listing.Entries, entry)
	}
	return listing, nil
}

// resolve is the folder a typed path names: ~ and ~/x are under the host's home, and the path is
// cleaned, so a trailing slash names the same folder.
func (l DirectoryLister) resolve(path string) (string, error) {
	switch {
	case path == "~" || strings.HasPrefix(path, "~/"):
		home, err := l.Tree.Home()
		if err != nil {
			return "", fmt.Errorf("home directory: %w", err)
		}
		path = filepath.Join(home, strings.TrimPrefix(path, "~"))
	case !filepath.IsAbs(path):
		return "", &daemon.DirectoryError{Fault: daemon.DirectoryNotAbsolute, Reason: fmt.Sprintf("path %q is not absolute and does not start with ~/", path)}
	}
	return filepath.Clean(path), nil
}
