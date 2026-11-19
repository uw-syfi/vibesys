package hostfs

import (
	"context"
	"errors"
	"io"
)

// ErrTooLarge is what ReadFile fails with for a file over the limit it was given.
var ErrTooLarge = errors.New("the file is larger than the limit")

// ErrNotRegular is what Open fails with for an entry that is not a regular file, or a path that
// leaves the directory.
var ErrNotRegular = errors.New("not a regular file inside the directory")

// FS is the host's file system as a job's flow uses it.
type FS interface {
	// MakeScratch creates an empty directory outside every checkout, and returns its absolute
	// path and the function that removes it with everything in it. The function is idempotent.
	MakeScratch(ctx context.Context) (dir string, remove func(), err error)

	// ReadFile returns the whole content of the file. It fails with an error that satisfies
	// errors.Is(err, fs.ErrNotExist) for a file that is not there and with ErrTooLarge for a file
	// of more than limit bytes, and in both cases returns no content.
	ReadFile(path string, limit int64) ([]byte, error)

	// MakeDir creates the directory at the absolute path, with its parents, and leaves it as it
	// is when it exists. It is how a job's artifacts directory is made, which outlives the job.
	MakeDir(ctx context.Context, path string) error

	// Tree lists every entry under the directory at the absolute path dir, at any depth, except the
	// directories themselves: each by its slash-separated path relative to dir, sorted. It never
	// follows a symbolic link, which it lists as one. A dir that does not exist lists nothing.
	Tree(ctx context.Context, dir string) ([]Entry, error)

	// Open opens the regular file at the relative path rel under the absolute directory dir. It
	// refuses a path that leaves dir, and a file that is not a regular one, a symbolic link
	// included, and never follows a link out of dir.
	Open(dir, rel string) (io.ReadCloser, error)
}

// Entry is one entry Tree lists.
type Entry struct {
	// Path is relative to the directory listed, slash-separated.
	Path string
	Kind EntryKind
	// Size is a regular file's size, and zero for any other entry.
	Size int64
}

// EntryKind is what an entry is. The set is closed.
type EntryKind int

// The kinds of entry Tree lists.
const (
	EntryFile EntryKind = iota + 1
	EntrySymlink
	// EntryOther is a device, a pipe, a socket or anything else that is neither a regular file, a
	// directory nor a link.
	EntryOther
)
