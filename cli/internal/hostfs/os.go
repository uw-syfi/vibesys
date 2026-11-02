package hostfs

import (
	"context"
	"fmt"
	"io"
	"os"
	"path/filepath"
)

// OS is the real file system. Scratch directories are made under Base, or the system's
// temporary directory when Base is empty.
type OS struct {
	Base string
}

var _ FS = OS{}

// MakeScratch implements FS.
func (o OS) MakeScratch(ctx context.Context) (string, func(), error) {
	if err := ctx.Err(); err != nil {
		return "", nil, err
	}
	dir, err := os.MkdirTemp(o.Base, "vibesys-scratch-")
	if err != nil {
		return "", nil, fmt.Errorf("making a scratch directory: %w", err)
	}
	if dir, err = filepath.Abs(dir); err != nil {
		return "", nil, err
	}
	return dir, func() { _ = os.RemoveAll(dir) }, nil
}

// ReadFile implements FS.
func (OS) ReadFile(path string, limit int64) ([]byte, error) {
	f, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer f.Close()
	data, err := io.ReadAll(io.LimitReader(f, limit+1))
	if err != nil {
		return nil, err
	}
	if int64(len(data)) > limit {
		return nil, ErrTooLarge
	}
	return data, nil
}
