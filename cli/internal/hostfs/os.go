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

// MakeDir implements FS.
func (OS) MakeDir(ctx context.Context, path string) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if !filepath.IsAbs(path) {
		return fmt.Errorf("making a directory: %q is not an absolute path", path)
	}
	if err := os.MkdirAll(path, 0o755); err != nil {
		return fmt.Errorf("making a directory: %w", err)
	}
	return nil
}

// ListDir implements FS. os.ReadDir returns the entries sorted by name.
func (OS) ListDir(path string) ([]string, error) {
	entries, err := os.ReadDir(path)
	if err != nil {
		return nil, err
	}
	names := make([]string, len(entries))
	for i, e := range entries {
		names[i] = e.Name()
	}
	return names, nil
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
