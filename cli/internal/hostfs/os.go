package hostfs

import (
	"context"
	"fmt"
	"io"
	"io/fs"
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

// Files implements FS.
func (OS) Files(root string) ([]File, error) {
	info, err := os.Stat(root)
	if err != nil {
		return nil, err
	}
	if info.Mode().IsRegular() {
		return []File{{Size: info.Size(), Executable: info.Mode()&0o100 != 0}}, nil
	}
	var out []File
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil || !d.Type().IsRegular() {
			return err
		}
		info, err := d.Info()
		if err != nil {
			return err
		}
		rel, err := filepath.Rel(root, p)
		if err != nil {
			return err
		}
		out = append(out, File{Path: filepath.ToSlash(rel), Size: info.Size(), Executable: info.Mode()&0o100 != 0})
		return nil
	})
	return out, err
}

// ReadAt implements FS.
func (OS) ReadAt(path string, p []byte, offset int64) (int, error) {
	f, err := os.Open(path)
	if err != nil {
		return 0, err
	}
	defer f.Close()
	return f.ReadAt(p, offset)
}

// WriteAt implements FS.
func (OS) WriteAt(ctx context.Context, path string, data []byte, offset int64, executable bool) error {
	if err := ctx.Err(); err != nil {
		return err
	}
	if !filepath.IsAbs(path) {
		return fmt.Errorf("writing a file: %q is not an absolute path", path)
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return fmt.Errorf("writing a file: %w", err)
	}
	mode := os.FileMode(0o644)
	if executable {
		mode = 0o755
	}
	flags := os.O_WRONLY | os.O_CREATE
	if offset == 0 {
		flags |= os.O_TRUNC
	}
	f, err := os.OpenFile(path, flags, mode)
	if err != nil {
		return fmt.Errorf("writing a file: %w", err)
	}
	defer f.Close()
	if info, err := f.Stat(); err != nil || info.Size() < offset {
		return fmt.Errorf("writing a file: %s is shorter than the offset %d", path, offset)
	}
	if _, err := f.WriteAt(data, offset); err != nil {
		return fmt.Errorf("writing a file: %w", err)
	}
	if err := f.Chmod(mode); err != nil {
		return fmt.Errorf("writing a file: %w", err)
	}
	return f.Close()
}
