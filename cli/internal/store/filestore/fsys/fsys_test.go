package fsys_test

import (
	"bytes"
	"errors"
	"fmt"
	"io/fs"
	"math/rand/v2"
	"path"
	"reflect"
	"slices"
	"strings"
	"syscall"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

// newFS builds an empty filesystem with a root directory to work in.
type newFS func(t *testing.T) (fsys.FS, string)

var implementations = map[string]newFS{
	"OS":    func(t *testing.T) (fsys.FS, string) { return fsys.OS{}, t.TempDir() },
	"MemFS": func(t *testing.T) (fsys.FS, string) { return fsys.NewMemFS(), "root" },
}

func mustMkdir(t *testing.T, f fsys.FS, name string) {
	t.Helper()
	if err := f.Mkdir(name); err != nil {
		t.Fatalf("Mkdir(%s): %v", name, err)
	}
}

func write(t *testing.T, f fsys.FS, name string, data []byte) {
	t.Helper()
	file, err := f.Create(name)
	if err != nil {
		t.Fatalf("Create(%s): %v", name, err)
	}
	if _, err := file.Write(data); err != nil {
		t.Fatalf("Write(%s): %v", name, err)
	}
	if err := file.Sync(); err != nil {
		t.Fatalf("Sync(%s): %v", name, err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("Close(%s): %v", name, err)
	}
}

// The contract every implementation passes.
func TestContract(t *testing.T) {
	for name, build := range implementations {
		t.Run(name, func(t *testing.T) {
			t.Run("a file reads back what was written, in pieces", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				file, err := f.Create(path.Join(root, "a"))
				if err != nil {
					t.Fatal(err)
				}
				for _, piece := range []string{"hello ", "", "world"} {
					if _, err := file.Write([]byte(piece)); err != nil {
						t.Fatal(err)
					}
				}
				if err := file.Sync(); err != nil {
					t.Fatal(err)
				}
				if err := file.Close(); err != nil {
					t.Fatal(err)
				}
				got, err := f.ReadFile(path.Join(root, "a"))
				if err != nil || string(got) != "hello world" {
					t.Errorf("ReadFile = %q, %v; want %q", got, err, "hello world")
				}
			})

			t.Run("create empties a file that is there", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				write(t, f, path.Join(root, "a"), []byte("long old content"))
				write(t, f, path.Join(root, "a"), []byte("new"))
				if got, err := f.ReadFile(path.Join(root, "a")); err != nil || string(got) != "new" {
					t.Errorf("ReadFile = %q, %v; want %q", got, err, "new")
				}
			})

			t.Run("a missing name is fs.ErrNotExist", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				missing := path.Join(root, "nope")
				under := path.Join(root, "nope", "child")
				checks := map[string]error{
					"ReadFile":         second(f.ReadFile(missing)),
					"ReadDir":          second(f.ReadDir(missing)),
					"Remove":           f.Remove(missing),
					"SyncDir":          f.SyncDir(missing),
					"Rename":           f.Rename(missing, path.Join(root, "other")),
					"Mkdir below":      f.Mkdir(under),
					"Create below":     second(f.Create(under)),
					"ReadFile below":   second(f.ReadFile(under)),
					"Rename to below":  f.Rename(missing, under),
					"ReadFile of root": nil,
				}
				for what, err := range checks {
					if what == "ReadFile of root" {
						continue
					}
					if !errors.Is(err, fs.ErrNotExist) {
						t.Errorf("%s: error = %v, want fs.ErrNotExist", what, err)
					}
				}
			})

			t.Run("mkdir of a name that is taken is fs.ErrExist", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				mustMkdir(t, f, path.Join(root, "d"))
				if err := f.Mkdir(path.Join(root, "d")); !errors.Is(err, fs.ErrExist) {
					t.Errorf("second Mkdir = %v, want fs.ErrExist", err)
				}
			})

			t.Run("rename replaces a file and leaves nothing under the old name", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				mustMkdir(t, f, path.Join(root, "sub"))
				write(t, f, path.Join(root, "old"), []byte("old"))
				write(t, f, path.Join(root, "sub", "tmp"), []byte("new"))
				if err := f.Rename(path.Join(root, "sub", "tmp"), path.Join(root, "old")); err != nil {
					t.Fatal(err)
				}
				if got, err := f.ReadFile(path.Join(root, "old")); err != nil || string(got) != "new" {
					t.Errorf("ReadFile = %q, %v; want %q", got, err, "new")
				}
				if _, err := f.ReadFile(path.Join(root, "sub", "tmp")); !errors.Is(err, fs.ErrNotExist) {
					t.Errorf("the old name is still there: %v", err)
				}
			})

			t.Run("directories list by name with their sizes", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				mustMkdir(t, f, path.Join(root, "b"))
				write(t, f, path.Join(root, "c"), []byte("12345"))
				write(t, f, path.Join(root, "a"), nil)
				got, err := f.ReadDir(root)
				want := []fsys.Entry{{Name: "a"}, {Name: "b", IsDir: true}, {Name: "c", Size: 5}}
				if err != nil || !reflect.DeepEqual(got, want) {
					t.Errorf("ReadDir = %+v, %v; want %+v", got, err, want)
				}
			})

			t.Run("remove deletes a file and an empty directory", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				mustMkdir(t, f, path.Join(root, "d"))
				write(t, f, path.Join(root, "d", "f"), []byte("x"))
				if err := f.Remove(path.Join(root, "d")); err == nil {
					t.Errorf("Remove of a directory with a file in it succeeded")
				}
				if err := f.Remove(path.Join(root, "d", "f")); err != nil {
					t.Fatal(err)
				}
				if err := f.Remove(path.Join(root, "d")); err != nil {
					t.Errorf("Remove of the empty directory: %v", err)
				}
				if got, _ := f.ReadDir(root); len(got) != 0 {
					t.Errorf("ReadDir = %+v, want empty", got)
				}
			})

			t.Run("sync of a directory that exists succeeds", func(t *testing.T) {
				f, root := build(t)
				if name == "MemFS" {
					mustMkdir(t, f, root)
				}
				if err := f.SyncDir(root); err != nil {
					t.Errorf("SyncDir: %v", err)
				}
			})
		})
	}
}

func second[T any](_ T, err error) error { return err }

func TestALockKeepsOutEveryOtherHold(t *testing.T) {
	for name, build := range implementations {
		t.Run(name, func(t *testing.T) {
			f, root := build(t)
			if name == "MemFS" {
				mustMkdir(t, f, root)
			}
			file := path.Join(root, "lock")
			first, err := f.Lock(file)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := f.Lock(file); !errors.Is(err, fsys.ErrLocked) {
				t.Fatalf("second Lock: error = %v, want ErrLocked", err)
			}
			for range 2 {
				if err := first.Release(); err != nil {
					t.Fatal(err)
				}
			}
			again, err := f.Lock(file)
			if err != nil {
				t.Fatalf("Lock after Release: %v", err)
			}
			defer func() { _ = again.Release() }()

			// A hold is on the file, so a new file of the same name is free. This is what a fence
			// beyond the lock is for.
			if err := f.Remove(file); err != nil {
				t.Fatal(err)
			}
			beside, err := f.Lock(file)
			if err != nil {
				t.Fatalf("Lock of a new file with the name of a held one: %v", err)
			}
			_ = beside.Release()

			if _, err := f.Lock(path.Join(root, "missing", "lock")); !errors.Is(err, fs.ErrNotExist) {
				t.Errorf("Lock in a missing directory: error = %v, want fs.ErrNotExist", err)
			}
		})
	}
}

// A filesystem where the lock call does nothing, or is refused, gets no lock at all, and the
// caller is told.
func TestALockThatKeepsNobodyOutIsRefused(t *testing.T) {
	calls := map[string]func(int, int) error{
		"a call that does nothing": func(int, int) error { return nil },
		"no lock records":          func(int, int) error { return syscall.ENOLCK },
		"not supported":            func(int, int) error { return syscall.ENOTSUP },
		"no such call":             func(int, int) error { return syscall.ENOSYS },
	}
	for name, call := range calls {
		t.Run(name, func(t *testing.T) {
			lock, err := fsys.OS{Flock: call}.Lock(path.Join(t.TempDir(), "lock"))
			if !errors.Is(err, fsys.ErrNoLocks) {
				t.Fatalf("Lock: %v, error = %v, want ErrNoLocks", lock, err)
			}
		})
	}
	t.Run("any other failure is that failure", func(t *testing.T) {
		_, err := fsys.OS{Flock: func(int, int) error { return syscall.EIO }}.Lock(path.Join(t.TempDir(), "lock"))
		if !errors.Is(err, syscall.EIO) || errors.Is(err, fsys.ErrNoLocks) {
			t.Fatalf("error = %v, want EIO", err)
		}
	})
}

func TestCheckMount(t *testing.T) {
	const table = `22 28 0:21 / /sys rw,nosuid shared:7 - sysfs sysfs rw
28 1 254:1 / / rw,relatime shared:1 - ext4 /dev/vda1 rw,errors=remount-ro
70 28 0:50 / /home rw,relatime shared:30 - nfs4 host-a:/home rw,vers=4.2,addr=10.0.0.1,local_lock=none
71 28 0:51 / /flock rw,relatime shared:31 - nfs host-a:/flock rw,vers=3,local_lock=flock
72 28 0:52 / /all rw,relatime - nfs host-a:/all rw,vers=3,local_lock=all
73 28 0:53 / /nolock rw,relatime - nfs4 host-a:/nolock rw,nolock
74 28 0:54 / /a\040b rw,relatime - ext4 /dev/vdb rw,nolock
broken line
`
	cases := []struct {
		major, minor uint32
		refused      bool
		mount        string
	}{
		{major: 254, minor: 1}, {major: 0, minor: 50}, {major: 0, minor: 54}, {major: 9, minor: 9},
		{minor: 51, refused: true, mount: "/flock"},
		{minor: 52, refused: true, mount: "/all"},
		{minor: 53, refused: true, mount: "/nolock"},
	}
	for _, c := range cases {
		err := fsys.CheckMount([]byte(table), c.major, c.minor)
		if got := errors.Is(err, fsys.ErrNoLocks); got != c.refused {
			t.Errorf("%d:%d: error = %v, want refused = %v", c.major, c.minor, err, c.refused)
		}
		if c.refused && !strings.Contains(err.Error(), c.mount) {
			t.Errorf("%d:%d: error %q does not name %s", c.major, c.minor, err, c.mount)
		}
	}
}

func FuzzCheckMount(f *testing.F) {
	f.Add([]byte("71 28 0:51 / /flock rw shared:31 - nfs host-a:/flock rw,local_lock=flock\n"), uint32(0), uint32(51))
	f.Add([]byte("- - - - - - - - -\n7 - \n\x00"), uint32(0), uint32(0))
	f.Fuzz(func(t *testing.T, table []byte, major, minor uint32) {
		if err := fsys.CheckMount(table, major, minor); err != nil && !errors.Is(err, fsys.ErrNoLocks) {
			t.Fatalf("error = %v, want nil or ErrNoLocks", err)
		}
	})
}

// Both implementations answer a random sequence of calls the same way.
func TestImplementationsAgree(t *testing.T) {
	dirs := []string{"", "d1", "d2", "d1/d3"}
	files := []string{"a", "b", "d1/x", "d2/z", "d1/d3/w", "q/missing"}
	anyName := append(slices.Clone(dirs[1:]), files...)
	for seed := range uint64(40) {
		rng := rand.New(rand.NewPCG(seed, 1))
		targets := []struct {
			f    fsys.FS
			root string
		}{{fsys.OS{}, t.TempDir()}, {fsys.NewMemFS(), "root"}}
		mustMkdir(t, targets[1].f, "root")
		for step := range 150 {
			op := rng.IntN(7)
			dir, file, other, either := dirs[rng.IntN(len(dirs))], files[rng.IntN(len(files))], files[rng.IntN(len(files))], anyName[rng.IntN(len(anyName))]
			data := bytes.Repeat([]byte{byte('a' + rng.IntN(26))}, rng.IntN(20))
			var answers [2]string
			for i, target := range targets {
				at := func(name string) string { return path.Join(target.root, name) }
				var out any
				var err error
				switch op {
				case 0:
					err = target.f.Mkdir(at(dir))
				case 1:
					var f fsys.File
					if f, err = target.f.Create(at(file)); err == nil {
						if _, err = f.Write(data); err == nil {
							err = f.Sync()
						}
					}
				case 2:
					out, err = target.f.ReadFile(at(either))
				case 3:
					out, err = target.f.ReadDir(at(either))
				case 4:
					err = target.f.Rename(at(file), at(other))
				case 5:
					err = target.f.Remove(at(either))
				default:
					err = target.f.SyncDir(at(dir))
				}
				answers[i] = describe(out, err)
			}
			if answers[0] != answers[1] {
				t.Fatalf("seed %d, step %d, op %d on %q %q %q %q: OS answered %s, MemFS answered %s", seed, step, op, dir, file, other, either, answers[0], answers[1])
			}
		}
	}
}

func describe(out any, err error) string {
	switch {
	case errors.Is(err, fs.ErrNotExist):
		return "does not exist"
	case errors.Is(err, fs.ErrExist):
		return "exists"
	case err != nil:
		return "fails"
	}
	return fmt.Sprintf("ok %q", out)
}
