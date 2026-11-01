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
