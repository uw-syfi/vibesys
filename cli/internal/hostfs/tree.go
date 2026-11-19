package hostfs

import (
	"context"
	"errors"
	"fmt"
	"io"
	"io/fs"
	"os"
	"path"
	"path/filepath"
	"sort"
)

// Tree implements FS. It reads the directory through an os.Root, so nothing it lists or reads is
// outside dir, and lists each entry by its own type, so a link is never followed.
func (OS) Tree(ctx context.Context, dir string) ([]Entry, error) {
	if !filepath.IsAbs(dir) {
		return nil, fmt.Errorf("listing a directory: %q is not an absolute path", dir)
	}
	root, err := os.OpenRoot(dir)
	if errors.Is(err, fs.ErrNotExist) {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("listing %s: %w", dir, err)
	}
	defer root.Close()
	var out []Entry
	err = fs.WalkDir(root.FS(), ".", func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if err := ctx.Err(); err != nil {
			return err
		}
		if p == "." {
			return nil
		}
		switch t := d.Type(); {
		case t.IsDir():
			return nil
		case t&fs.ModeSymlink != 0:
			out = append(out, Entry{Path: p, Kind: EntrySymlink})
		case t.IsRegular():
			info, err := d.Info()
			if err != nil {
				return err
			}
			out = append(out, Entry{Path: p, Kind: EntryFile, Size: info.Size()})
		default:
			out = append(out, Entry{Path: p, Kind: EntryOther})
		}
		return nil
	})
	if err != nil {
		return nil, fmt.Errorf("listing %s: %w", dir, err)
	}
	sort.Slice(out, func(i, j int) bool { return out[i].Path < out[j].Path })
	return out, nil
}

// Open implements FS. The file it opens is the one it found regular without following a link:
// the two are compared after the open, so an entry swapped for a link in between is refused.
func (OS) Open(dir, rel string) (io.ReadCloser, error) {
	if !filepath.IsAbs(dir) || path.Clean(rel) != rel || !filepath.IsLocal(filepath.FromSlash(rel)) {
		return nil, fmt.Errorf("opening %q in %q: %w", rel, dir, ErrNotRegular)
	}
	root, err := os.OpenRoot(dir)
	if err != nil {
		return nil, err
	}
	defer root.Close()
	name := filepath.FromSlash(rel)
	// Each parent is a directory and not a link, as Tree lists them.
	for p := path.Dir(rel); p != "."; p = path.Dir(p) {
		info, err := root.Lstat(filepath.FromSlash(p))
		if err != nil {
			return nil, err
		}
		if !info.IsDir() {
			return nil, fmt.Errorf("opening %q in %q: %s is not a directory: %w", rel, dir, p, ErrNotRegular)
		}
	}
	before, err := root.Lstat(name)
	if err != nil {
		return nil, err
	}
	if !before.Mode().IsRegular() {
		return nil, fmt.Errorf("opening %q in %q: %w", rel, dir, ErrNotRegular)
	}
	f, err := root.Open(name)
	if err != nil {
		return nil, err
	}
	after, err := f.Stat()
	if err != nil || !os.SameFile(before, after) {
		f.Close()
		return nil, fmt.Errorf("opening %q in %q: it changed while it was opened: %w", rel, dir, ErrNotRegular)
	}
	return f, nil
}
