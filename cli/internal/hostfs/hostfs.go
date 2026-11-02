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
}
