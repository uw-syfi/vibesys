package hostfs_test

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
)

// treeWorld is a file system with a way to put a file and a symbolic link in it, as a job's
// command would.
type treeWorld struct {
	fsys  hostfs.FS
	write func(t *testing.T, path string, data []byte)
	link  func(t *testing.T, path, target string)
	// dir is an absolute directory the world may write under.
	dir string
}

// treeContract is what every FS does when it lists and opens what a job left.
func treeContract(t *testing.T, newWorld func(t *testing.T) treeWorld) {
	ctx := context.Background()
	t.Run("a tree lists files at any depth by relative path, sorted, without directories", func(t *testing.T) {
		w := newWorld(t)
		w.write(t, w.dir+"/out/b.json", []byte("bb"))
		w.write(t, w.dir+"/out/a/deep/x.csv", []byte("x"))
		w.write(t, w.dir+"/out/.hidden", nil)
		w.write(t, w.dir+"/elsewhere", []byte("no"))
		got, err := w.fsys.Tree(ctx, w.dir+"/out")
		if err != nil {
			t.Fatal(err)
		}
		want := []hostfs.Entry{
			{Path: ".hidden", Kind: hostfs.EntryFile, Size: 0},
			{Path: "a/deep/x.csv", Kind: hostfs.EntryFile, Size: 1},
			{Path: "b.json", Kind: hostfs.EntryFile, Size: 2},
		}
		if !slices.Equal(got, want) {
			t.Errorf("Tree = %+v, want %+v", got, want)
		}
	})
	t.Run("a directory that is not there lists nothing", func(t *testing.T) {
		w := newWorld(t)
		got, err := w.fsys.Tree(ctx, w.dir+"/missing")
		if err != nil || len(got) != 0 {
			t.Errorf("Tree = %+v, %v", got, err)
		}
	})
	t.Run("a link is listed as one and never followed, whatever it points at", func(t *testing.T) {
		w := newWorld(t)
		w.write(t, w.dir+"/secret/key", []byte("secret"))
		w.write(t, w.dir+"/out/real", []byte("ok"))
		w.link(t, w.dir+"/out/to-file", w.dir+"/secret/key")
		w.link(t, w.dir+"/out/to-dir", w.dir+"/secret")
		w.link(t, w.dir+"/out/up", "../secret/key")
		got, err := w.fsys.Tree(ctx, w.dir+"/out")
		if err != nil {
			t.Fatal(err)
		}
		want := []hostfs.Entry{
			{Path: "real", Kind: hostfs.EntryFile, Size: 2},
			{Path: "to-dir", Kind: hostfs.EntrySymlink},
			{Path: "to-file", Kind: hostfs.EntrySymlink},
			{Path: "up", Kind: hostfs.EntrySymlink},
		}
		if !slices.Equal(got, want) {
			t.Errorf("Tree = %+v, want %+v", got, want)
		}
		for _, rel := range []string{"to-file", "to-dir/key", "up", "../secret/key", "/etc/passwd", "./real", "real/", ""} {
			if r, err := w.fsys.Open(w.dir+"/out", rel); err == nil {
				r.Close()
				t.Errorf("Open(%q) opened it", rel)
			}
		}
	})
	t.Run("open reads a regular file whole", func(t *testing.T) {
		w := newWorld(t)
		w.write(t, w.dir+"/out/a/b.txt", []byte("hello"))
		r, err := w.fsys.Open(w.dir+"/out", "a/b.txt")
		if err != nil {
			t.Fatal(err)
		}
		defer r.Close()
		if data, err := io.ReadAll(r); err != nil || string(data) != "hello" {
			t.Errorf("read %q, %v", data, err)
		}
		if _, err := w.fsys.Open(w.dir+"/out", "a/none"); !errors.Is(err, fs.ErrNotExist) {
			t.Errorf("a missing file: %v", err)
		}
	})
}

func TestOSPassesTheTreeContract(t *testing.T) {
	treeContract(t, func(t *testing.T) treeWorld {
		return treeWorld{
			fsys: hostfs.OS{},
			dir:  t.TempDir(),
			write: func(t *testing.T, p string, data []byte) {
				if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.WriteFile(p, data, 0o600); err != nil {
					t.Fatal(err)
				}
			},
			link: func(t *testing.T, p, target string) {
				if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
					t.Fatal(err)
				}
				if err := os.Symlink(target, p); err != nil {
					t.Fatal(err)
				}
			},
		}
	})
}

func TestMemoryPassesTheTreeContract(t *testing.T) {
	treeContract(t, func(*testing.T) treeWorld {
		m := hostfs.NewMemory()
		return treeWorld{
			fsys:  m,
			dir:   "/memory/world",
			write: func(_ *testing.T, p string, data []byte) { m.Put(p, data) },
			link:  func(_ *testing.T, p, target string) { m.PutLink(p, target) },
		}
	})
}

// A file or a fifo that is not regular is listed as other, and never opened. The memory file
// system has no such entries, so only the real one is asked.
func TestOSListsAFifoAsOther(t *testing.T) {
	dir := t.TempDir()
	if err := syscall.Mkfifo(filepath.Join(dir, "pipe"), 0o600); err != nil {
		t.Skipf("no fifo here: %v", err)
	}
	got, err := hostfs.OS{}.Tree(context.Background(), dir)
	if err != nil || !slices.Equal(got, []hostfs.Entry{{Path: "pipe", Kind: hostfs.EntryOther}}) {
		t.Errorf("Tree = %+v, %v", got, err)
	}
	if r, err := (hostfs.OS{}).Open(dir, "pipe"); err == nil {
		r.Close()
		t.Error("opened a fifo")
	}
}
