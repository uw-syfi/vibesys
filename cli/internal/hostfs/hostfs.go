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

	// ListDir returns the names of the entries directly in the directory, files and directories
	// alike, sorted. It fails with an error that satisfies errors.Is(err, fs.ErrNotExist) for a
	// directory that is not there.
	ListDir(path string) ([]string, error)
}
