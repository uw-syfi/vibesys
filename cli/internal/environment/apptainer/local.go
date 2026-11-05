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
	// archive it reads from archive, and then writes the SIF to sif. Its temporary files
	// (APPTAINER_TMPDIR, APPTAINER_CACHEDIR) are in that directory. A Build that fails writes
	// nothing to sif, or returns an error after what it wrote.
	Build(ctx context.Context, dir string, archive io.Reader, sif io.Writer) error
	// Describe says which program this is, with its version, for a person: what built a SIF.
	Describe(ctx context.Context) (string, error)
	// Lock takes, without waiting, the lock of the conversions of one image on this machine,
	// named by name (a file name, with no slash), in dir, which it makes. Every process of the
	// machine shares the lock: one that another holds, in this process or another, returns
	// ErrConverting at once. Closing what it returns lets the lock go, and so does the end of the
	// process, however it ends.
	Lock(ctx context.Context, dir, name string) (io.Closer, error)
}

// ErrConverting says that another conversion of the image on this machine holds its lock.
var ErrConverting = errors.New("apptainer: another conversion of the image on this machine holds its lock")
