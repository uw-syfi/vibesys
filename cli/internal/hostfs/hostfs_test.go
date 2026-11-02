package hostfs_test

import (
	"context"
	"errors"
	"io/fs"
	"os"
	"path/filepath"
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
