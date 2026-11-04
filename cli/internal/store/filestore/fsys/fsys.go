// Package fsys is the slice of a filesystem the file store uses, and two implementations: OS, the
// real one, and MemFS, an in-memory one that can be told to fail or die at any write step.
//
// The interface is as narrow as the file store's commit protocol: create a file, write, sync,
// rename it over its final name, and sync the directory. Nothing in it can append to a file or
// change one in place, so a caller cannot write a torn record by using it as intended. Each
// method is atomic or has no effect that outlives the process, on a local filesystem and on NFS:
// rename within a directory, exclusive directory creation, unlink, and fsync. Lock is the one
// method whose strength depends on the mount; see OS.Lock.
package fsys

import (
	"errors"
	"io/fs"
	"os"
)

// ErrLocked says another hold has the file.
var ErrLocked = errors.New("the file is locked")

// ErrNoLocks says a lock here would not keep anyone out, so none was taken.
var ErrNoLocks = errors.New("locks here keep nobody out")

// Entry is one name in a directory.
type Entry struct {
	Name  string
	Size  int64
	IsDir bool
}

// File is a file open for writing from its start.
type File interface {
	// Write appends to what this handle has written. Nothing is durable until Sync returns.
	Write(p []byte) (int, error)
	// Sync makes what the handle wrote durable.
	Sync() error
	// Close releases the handle. It makes nothing durable.
	Close() error
}

// Lock is a hold on a file. It ends at Release or when the process dies, however it dies.
type Lock interface {
	// Release ends the hold. A second Release does nothing.
	Release() error
}

// FS is a filesystem. Names are slash-separated paths. An error satisfies errors.Is for
// fs.ErrNotExist when a name is missing, and for fs.ErrExist when Mkdir finds the name taken.
type FS interface {
	// ReadFile returns the whole file.
	ReadFile(name string) ([]byte, error)

	// ReadDir lists a directory, ordered by name.
	ReadDir(name string) ([]Entry, error)

	// Mkdir creates one directory. Its parent must exist. The new entry is durable only after
	// SyncDir of the parent.
	Mkdir(name string) error

	// Create makes a file with no content, or empties the one that is there, and opens it. The
	// new entry is durable only after SyncDir of the directory.
	Create(name string) (File, error)

	// Rename moves a file to a new name, replacing a file already there, atomically: a reader and
	// a crash see the old content or the new, never neither. It is durable only after SyncDir of
	// the directory.
	Rename(oldname, newname string) error

	// Remove deletes a file or an empty directory.
	Remove(name string) error

	// SyncDir makes the entries of a directory durable.
	SyncDir(name string) error

	// Lock makes the file if it is missing and takes the hold on it, at once or not at all: it
	// fails with ErrLocked while any other hold has the file, in this process or another, and
	// with ErrNoLocks where a hold would keep nobody out. A hold belongs to the file and not to
	// its name: one on a file that was then removed keeps nobody out of a new file of that name.
	Lock(name string) (Lock, error)
}

// OS is the filesystem of the operating system.
type OS struct {
	// Flock is the system call behind Lock. Nil is the real one; a test gives one that fails, or
	// one that does nothing, as a filesystem without working locks does.
	Flock func(fd int, how int) error
}

var _ FS = OS{}

// ReadFile implements FS.
func (OS) ReadFile(name string) ([]byte, error) { return os.ReadFile(name) }

// ReadDir implements FS.
func (OS) ReadDir(name string) ([]Entry, error) {
	entries, err := os.ReadDir(name)
	if err != nil {
		return nil, err
	}
	out := make([]Entry, 0, len(entries))
	for _, entry := range entries {
		e := Entry{Name: entry.Name(), IsDir: entry.IsDir()}
		if !e.IsDir {
			info, err := entry.Info()
			if err != nil {
				return nil, err
			}
			e.Size = info.Size()
		}
		out = append(out, e)
	}
	return out, nil
}

// Mkdir implements FS.
func (OS) Mkdir(name string) error { return os.Mkdir(name, 0o755) }

// Create implements FS.
func (OS) Create(name string) (File, error) {
	return os.OpenFile(name, os.O_WRONLY|os.O_CREATE|os.O_TRUNC, 0o644)
}

// Rename implements FS.
func (OS) Rename(oldname, newname string) error { return os.Rename(oldname, newname) }

// Remove implements FS.
func (OS) Remove(name string) error { return os.Remove(name) }

// SyncDir implements FS.
func (OS) SyncDir(name string) error {
	dir, err := os.Open(name)
	if err != nil {
		return err
	}
	if err := dir.Sync(); err != nil {
		_ = dir.Close()
		return &fs.PathError{Op: "sync", Path: name, Err: err}
	}
	return dir.Close()
}
