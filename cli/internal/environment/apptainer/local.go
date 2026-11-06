package apptainer

import (
	"context"
	"errors"
	"io"
)

// Converter builds SIFs on the caller's machine, where Docker built the images, with its apptainer
// program: the cluster's apptainer only runs them. The real one runs the program (localsif); a
// Fake builds SIFs in memory.
type Converter interface {
	// Free returns the bytes free in the directory, which it makes.
	Free(ctx context.Context, dir string) (int64, error)
	// Build builds, in a directory of its own under dir that it removes, the SIF of the docker
	// archive it reads from archive, and keeps it in dir as the SIF of the name (a file name, with
	// no slash), put there whole by a rename, in place of any it kept of the name. Its temporary
	// files (APPTAINER_TMPDIR, APPTAINER_CACHEDIR) are in its own directory. A Build that fails
	// keeps nothing new. A Build removes the SIFs kept in dir that are more than a day old first.
	Build(ctx context.Context, dir string, archive io.Reader, name string) error
	// Open returns the SIF of the name kept in dir, and false when there is none.
	Open(ctx context.Context, dir, name string) (io.ReadCloser, bool, error)
	// Remove removes the SIF of the name kept in dir. One there is none of is removed.
	Remove(ctx context.Context, dir, name string) error
	// Describe says which program this is, with its version, for a person: what built a SIF.
	Describe(ctx context.Context) (string, error)
	// Lock takes, without waiting, the lock of the conversions of one image on this machine,
	// named by name (a file name, with no slash), in dir, which it makes. Every process of the
	// machine shares the lock: one that another holds, in this process or another, returns
	// ErrConverting at once. Closing what it returns lets the lock go, and so does the end of the
	// process, however it ends.
	Lock(ctx context.Context, dir, name string) (io.Closer, error)
}

// ErrConnectionLost is wrapped by an error of a Cluster call whose connection to the cluster failed
// (ssh's own exit status 255), so the call did not happen as far as anyone saw and may succeed if
// made again.
var ErrConnectionLost = errors.New("apptainer: the connection to the cluster was lost")

// ErrConverting says that another conversion of the image on this machine holds its lock.
var ErrConverting = errors.New("apptainer: another conversion of the image on this machine holds its lock")
