package hostfs

import (
	"context"
	"errors"
)

// ErrTooLarge is what ReadFile fails with for a file over the limit it was given.
var ErrTooLarge = errors.New("the file is larger than the limit")

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

	// Files returns the regular files at or under the absolute path, sorted by their path from it:
	// the one file, with the empty path, when the path is a file. It fails with an error that
	// satisfies errors.Is(err, fs.ErrNotExist) for a path that is not there. Links and other
	// files that are not regular are left out.
	Files(path string) ([]File, error)

	// ReadAt reads from the file at the offset as io.ReaderAt does: io.EOF at its end.
	ReadAt(path string, p []byte, offset int64) (int, error)

	// WriteAt writes data into the file at the offset, making the file and its directories when
	// they are missing. A write at offset 0 starts the file afresh, and a write past its end fails.
	// executable makes the file one its owner may run.
	WriteAt(ctx context.Context, path string, data []byte, offset int64, executable bool) error
}

// File is a regular file Files found.
type File struct {
	// Path is the file's path from the path Files was given, with / between its parts.
	Path       string
	Size       int64
	Executable bool
}
