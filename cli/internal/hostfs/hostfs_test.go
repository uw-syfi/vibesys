package hostfs_test

import (
	"context"
	"errors"
	"io"
	"io/fs"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/hostfs"
)

// contract is what every FS does. write puts a file at a path the way a benchmark would.
func contract(t *testing.T, newFS func(t *testing.T) hostfs.FS, write func(t *testing.T, fsys hostfs.FS, path string, data []byte)) {
	ctx := context.Background()
	t.Run("a scratch directory is absolute, fresh and removable", func(t *testing.T) {
		fsys := newFS(t)
		a, removeA, err := fsys.MakeScratch(ctx)
		if err != nil {
			t.Fatal(err)
		}
		b, removeB, err := fsys.MakeScratch(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if !strings.HasPrefix(a, "/") || a == b {
			t.Fatalf("directories %q and %q", a, b)
		}
		write(t, fsys, a+"/x.out", []byte("one"))
		write(t, fsys, b+"/x.out", []byte("two"))
		removeA()
		removeA() // idempotent
		if _, err := fsys.ReadFile(a+"/x.out", 100); !errors.Is(err, fs.ErrNotExist) {
			t.Errorf("a removed directory still has its file: %v", err)
		}
		if got, err := fsys.ReadFile(b+"/x.out", 100); err != nil || string(got) != "two" {
			t.Errorf("removing one directory touched another: %q, %v", got, err)
		}
		removeB()
	})
	t.Run("reading a file returns all of it, within the limit", func(t *testing.T) {
		fsys := newFS(t)
		dir, remove, _ := fsys.MakeScratch(ctx)
		defer remove()
		write(t, fsys, dir+"/a", []byte("hello"))
		write(t, fsys, dir+"/empty", nil)
		for _, c := range []struct {
			name, want string
			limit      int64
		}{{"a", "hello", 5}, {"a", "hello", 1000}, {"empty", "", 0}} {
			if got, err := fsys.ReadFile(dir+"/"+c.name, c.limit); err != nil || string(got) != c.want {
				t.Errorf("%s with limit %d: %q, %v", c.name, c.limit, got, err)
			}
		}
		got, err := fsys.ReadFile(dir+"/a", 4)
		if !errors.Is(err, hostfs.ErrTooLarge) || got != nil {
			t.Errorf("a file over the limit: %q, %v", got, err)
		}
	})
	t.Run("a file that is not there is not found", func(t *testing.T) {
		fsys := newFS(t)
		dir, remove, _ := fsys.MakeScratch(ctx)
		defer remove()
		got, err := fsys.ReadFile(dir+"/missing", 10)
		if !errors.Is(err, fs.ErrNotExist) || got != nil {
			t.Errorf("%q, %v", got, err)
		}
	})
	t.Run("a file written in pieces reads back whole, and a write at 0 starts it afresh", func(t *testing.T) {
		fsys := newFS(t)
		dir, remove, _ := fsys.MakeScratch(ctx)
		defer remove()
		at := dir + "/deep/er/f"
		for _, w := range []struct {
			data   string
			offset int64
		}{{"hello", 0}, {" world", 5}, {"HELLO", 0}, {"!", 5}} {
			if err := fsys.WriteAt(ctx, at, []byte(w.data), w.offset, w.offset == 0); err != nil {
				t.Fatal(err)
			}
		}
		if got, err := fsys.ReadFile(at, 100); err != nil || string(got) != "HELLO!" {
			t.Errorf("read back %q, %v", got, err)
		}
		if err := fsys.WriteAt(ctx, at, []byte("x"), 99, false); err == nil {
			t.Error("a write past the end of the file was taken")
		}
		buf := make([]byte, 4)
		if n, err := fsys.ReadAt(at, buf, 2); n != 4 || err != nil || string(buf) != "LLO!" {
			t.Errorf("ReadAt at 2: %d %q, %v", n, buf[:n], err)
		}
		if n, err := fsys.ReadAt(at, buf, 4); n != 2 || !errors.Is(err, io.EOF) {
			t.Errorf("ReadAt to the end: %d, %v", n, err)
		}
	})
	t.Run("files lists the regular files under a directory, and a file as itself", func(t *testing.T) {
		fsys := newFS(t)
		dir, remove, _ := fsys.MakeScratch(ctx)
		defer remove()
		for _, p := range []string{"b/c", "a", "b/d/e"} {
			if err := fsys.WriteAt(ctx, dir+"/out/"+p, []byte(p), 0, p == "a"); err != nil {
				t.Fatal(err)
			}
		}
		got, err := fsys.Files(dir + "/out")
		want := []hostfs.File{{Path: "a", Size: 1, Executable: true}, {Path: "b/c", Size: 3}, {Path: "b/d/e", Size: 5}}
		if err != nil || !slices.Equal(got, want) {
			t.Errorf("Files = %v, %v; want %v", got, err, want)
		}
		if got, err := fsys.Files(dir + "/out/b/c"); err != nil || !slices.Equal(got, []hostfs.File{{Size: 3}}) {
			t.Errorf("Files of a file = %v, %v", got, err)
		}
		if _, err := fsys.Files(dir + "/none"); !errors.Is(err, fs.ErrNotExist) {
			t.Errorf("Files of nothing: %v", err)
		}
	})
	t.Run("a cancelled context makes no directory", func(t *testing.T) {
		fsys := newFS(t)
		done, cancel := context.WithCancel(ctx)
		cancel()
		if _, remove, err := fsys.MakeScratch(done); err == nil {
			remove()
			t.Error("made a directory for a cancelled context")
		}
	})
}

func TestOSPassesTheContract(t *testing.T) {
	contract(t,
		func(t *testing.T) hostfs.FS { return hostfs.OS{Base: t.TempDir()} },
		func(t *testing.T, _ hostfs.FS, path string, data []byte) {
			if err := os.WriteFile(filepath.Clean(path), data, 0o600); err != nil {
				t.Fatal(err)
			}
		})
}

func TestMemoryPassesTheContract(t *testing.T) {
	contract(t,
		func(*testing.T) hostfs.FS { return hostfs.NewMemory() },
		func(_ *testing.T, f hostfs.FS, path string, data []byte) { f.(*hostfs.Memory).Put(path, data) })
}

func TestMemoryReportsTheScratchDirectoriesLeftBehind(t *testing.T) {
	m := hostfs.NewMemory()
	_, remove, _ := m.MakeScratch(context.Background())
	if len(m.Scratch()) != 1 {
		t.Fatal(m.Scratch())
	}
	remove()
	if len(m.Scratch()) != 0 {
		t.Fatal(m.Scratch())
	}
}
