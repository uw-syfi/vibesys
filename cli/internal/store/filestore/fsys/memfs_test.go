package fsys_test

import (
	"errors"
	"io/fs"
	"maps"
	"math/rand/v2"
	"reflect"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

func files(t *testing.T, m *fsys.MemFS) map[string]string {
	t.Helper()
	out := map[string]string{}
	for name, data := range m.Files() {
		out[name] = string(data)
	}
	return out
}

func writeNoSync(t *testing.T, f fsys.FS, name, data string) {
	t.Helper()
	file, err := f.Create(name)
	if err != nil {
		t.Fatalf("Create(%s): %v", name, err)
	}
	if _, err := file.Write([]byte(data)); err != nil {
		t.Fatalf("Write(%s): %v", name, err)
	}
	if err := file.Close(); err != nil {
		t.Fatalf("Close(%s): %v", name, err)
	}
}

func TestMemFSWhatACrashKeeps(t *testing.T) {
	t.Run("a process kill keeps everything written, a power loss only what was synced", func(t *testing.T) {
		m := fsys.NewMemFS()
		mustMkdir(t, m, "d")
		writeNoSync(t, m, "d/loose", "loose")
		if got, want := files(t, m.CrashImage(false)), map[string]string{"d/loose": "loose"}; !reflect.DeepEqual(got, want) {
			t.Errorf("after a process kill: %v, want %v", got, want)
		}
		if got := files(t, m.CrashImage(true)); len(got) != 0 {
			t.Errorf("after a power loss: %v, want nothing, the directory entry was never synced", got)
		}
	})

	t.Run("a synced file is lost with its directory entry until the directory is synced", func(t *testing.T) {
		m := fsys.NewMemFS()
		write(t, m, "f", []byte("data"))
		if got := files(t, m.CrashImage(true)); len(got) != 0 {
			t.Errorf("before SyncDir: %v, want nothing", got)
		}
		if err := m.SyncDir("."); err != nil {
			t.Fatal(err)
		}
		if got, want := files(t, m.CrashImage(true)), map[string]string{"f": "data"}; !reflect.DeepEqual(got, want) {
			t.Errorf("after SyncDir: %v, want %v", got, want)
		}
	})

	t.Run("an entry that is durable keeps only the content that was synced", func(t *testing.T) {
		m := fsys.NewMemFS()
		write(t, m, "f", []byte("synced"))
		if err := m.SyncDir("."); err != nil {
			t.Fatal(err)
		}
		file, err := m.Create("g")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := file.Write([]byte("unsynced")); err != nil {
			t.Fatal(err)
		}
		if err := m.SyncDir("."); err != nil {
			t.Fatal(err)
		}
		want := map[string]string{"f": "synced", "g": ""}
		if got := files(t, m.CrashImage(true)); !reflect.DeepEqual(got, want) {
			t.Errorf("after a power loss: %v, want %v", got, want)
		}
	})

	t.Run("a rename is durable only after the directory is synced", func(t *testing.T) {
		m := fsys.NewMemFS()
		write(t, m, "name", []byte("old"))
		if err := m.SyncDir("."); err != nil {
			t.Fatal(err)
		}
		write(t, m, "tmp", []byte("new"))
		if err := m.Rename("tmp", "name"); err != nil {
			t.Fatal(err)
		}
		if got, want := files(t, m.CrashImage(true)), map[string]string{"name": "old"}; !reflect.DeepEqual(got, want) {
			t.Errorf("before SyncDir: %v, want %v", got, want)
		}
		if got, want := files(t, m.CrashImage(false)), map[string]string{"name": "new"}; !reflect.DeepEqual(got, want) {
			t.Errorf("after a process kill: %v, want %v", got, want)
		}
		if err := m.SyncDir("."); err != nil {
			t.Fatal(err)
		}
		if got, want := files(t, m.CrashImage(true)), map[string]string{"name": "new"}; !reflect.DeepEqual(got, want) {
			t.Errorf("after SyncDir: %v, want %v", got, want)
		}
	})

	t.Run("a directory and what is in it need their own syncs", func(t *testing.T) {
		m := fsys.NewMemFS()
		mustMkdir(t, m, "d")
		write(t, m, "d/f", []byte("x"))
		if err := m.SyncDir("d"); err != nil {
			t.Fatal(err)
		}
		if got := files(t, m.CrashImage(true)); len(got) != 0 {
			t.Errorf("the parent was not synced, yet %v survived", got)
		}
		if err := m.SyncDir("."); err != nil {
			t.Fatal(err)
		}
		if got, want := files(t, m.CrashImage(true)), map[string]string{"d/f": "x"}; !reflect.DeepEqual(got, want) {
			t.Errorf("after both syncs: %v, want %v", got, want)
		}
	})

	t.Run("a crash image is independent of the filesystem it came from", func(t *testing.T) {
		m := fsys.NewMemFS()
		write(t, m, "f", []byte("a"))
		image := m.CrashImage(false)
		write(t, m, "g", []byte("b"))
		write(t, image, "h", []byte("c"))
		if got := files(t, m); !reflect.DeepEqual(got, map[string]string{"f": "a", "g": "b"}) {
			t.Errorf("the original changed: %v", got)
		}
		if got := files(t, image); !reflect.DeepEqual(got, map[string]string{"f": "a", "h": "c"}) {
			t.Errorf("the image changed: %v", got)
		}
	})
}

// A program that syncs every file and then every directory loses nothing to a power loss.
func TestMemFSSyncedWorkSurvivesPowerLoss(t *testing.T) {
	names := []string{"a", "b", "d/x", "d/y", "e/z"}
	for seed := range uint64(60) {
		rng := rand.New(rand.NewPCG(seed, 7))
		m := fsys.NewMemFS()
		for range 40 {
			switch rng.IntN(4) {
			case 0:
				_ = m.Mkdir([]string{"d", "e"}[rng.IntN(2)])
			case 1:
				if f, err := m.Create(names[rng.IntN(len(names))]); err == nil {
					_, _ = f.Write([]byte{byte('a' + rng.IntN(26))})
					_ = f.Sync()
					_ = f.Close()
				}
			case 2:
				_ = m.Rename(names[rng.IntN(len(names))], names[rng.IntN(len(names))])
			default:
				_ = m.Remove(names[rng.IntN(len(names))])
			}
		}
		for _, dir := range []string{"d", "e", "."} {
			_ = m.SyncDir(dir)
		}
		if got, want := files(t, m.CrashImage(true)), files(t, m.CrashImage(false)); !maps.Equal(got, want) {
			t.Fatalf("seed %d: a power loss left %v, a process kill %v", seed, got, want)
		}
	}
}

// program takes five write steps: Mkdir, Create, Write, Sync, SyncDir.
func program(m *fsys.MemFS) error {
	if err := m.Mkdir("d"); err != nil {
		return err
	}
	f, err := m.Create("d/f")
	if err != nil {
		return err
	}
	if _, err := f.Write([]byte("abcd")); err != nil {
		return err
	}
	if err := f.Sync(); err != nil {
		return err
	}
	return m.SyncDir("d")
}

func TestMemFSFaults(t *testing.T) {
	const steps = 5

	t.Run("steps count write calls, not reads or closes", func(t *testing.T) {
		m := fsys.NewMemFS()
		if err := program(m); err != nil {
			t.Fatal(err)
		}
		_, _ = m.ReadFile("d/f")
		_, _ = m.ReadDir("d")
		if got := m.Steps(); got != steps {
			t.Errorf("Steps = %d, want %d", got, steps)
		}
	})

	t.Run("a crash stops the step and every call after it", func(t *testing.T) {
		for at := range steps {
			m := fsys.NewMemFS()
			m.SetFault(fsys.Fault{At: at, Kind: fsys.FaultCrash})
			if err := program(m); !errors.Is(err, fsys.ErrCrashed) {
				t.Fatalf("crash at %d: error = %v, want ErrCrashed", at, err)
			}
			if _, err := m.ReadFile("d/f"); !errors.Is(err, fsys.ErrCrashed) {
				t.Errorf("crash at %d: a read after the crash = %v, want ErrCrashed", at, err)
			}
			if _, err := m.ReadDir("."); !errors.Is(err, fsys.ErrCrashed) {
				t.Errorf("crash at %d: a listing after the crash = %v, want ErrCrashed", at, err)
			}
			if err := m.Mkdir("other"); !errors.Is(err, fsys.ErrCrashed) {
				t.Errorf("crash at %d: a write after the crash = %v, want ErrCrashed", at, err)
			}
			if got := m.Steps(); got != at+1 {
				t.Errorf("crash at %d: Steps = %d, want %d", at, got, at+1)
			}
			// The step that crashed did not happen.
			image := m.CrashImage(false)
			names := slices.Sorted(maps.Keys(image.Files()))
			want := map[int][]string{0: nil, 1: nil, 2: {"d/f"}, 3: {"d/f"}, 4: {"d/f"}}[at]
			if !slices.Equal(names, want) {
				t.Errorf("crash at %d: files after a process kill %v, want %v", at, names, want)
			}
			// The image is a running filesystem.
			if err := image.Mkdir("after"); err != nil {
				t.Errorf("crash at %d: the image does not work: %v", at, err)
			}
		}
	})

	t.Run("a torn write leaves half of its bytes", func(t *testing.T) {
		m := fsys.NewMemFS()
		m.SetFault(fsys.Fault{At: 2, Kind: fsys.FaultTornWrite})
		if err := program(m); !errors.Is(err, fsys.ErrCrashed) {
			t.Fatalf("error = %v, want ErrCrashed", err)
		}
		if got := files(t, m.CrashImage(false))["d/f"]; got != "ab" {
			t.Errorf("file after a torn write = %q, want %q", got, "ab")
		}
	})

	t.Run("a torn write fault at a step that is not a write is a crash", func(t *testing.T) {
		m := fsys.NewMemFS()
		m.SetFault(fsys.Fault{At: 0, Kind: fsys.FaultTornWrite})
		if err := program(m); !errors.Is(err, fsys.ErrCrashed) {
			t.Fatalf("error = %v, want ErrCrashed", err)
		}
		if got := files(t, m.CrashImage(false)); len(got) != 0 {
			t.Errorf("files = %v, want none", got)
		}
	})

	t.Run("a failure leaves the step undone and the filesystem working", func(t *testing.T) {
		for at := range steps {
			m := fsys.NewMemFS()
			m.SetFault(fsys.Fault{At: at, Kind: fsys.FaultFail})
			if err := program(m); !errors.Is(err, fsys.ErrInjected) {
				t.Fatalf("failure at %d: error = %v, want ErrInjected", at, err)
			}
			if err := m.Mkdir("other"); err != nil {
				t.Errorf("failure at %d: a later call failed: %v", at, err)
			}
			if at == 0 {
				if _, err := m.ReadDir("d"); !errors.Is(err, fs.ErrNotExist) {
					t.Errorf("failure at 0: the directory exists: %v", err)
				}
			}
		}
	})

	t.Run("a failure after the step leaves the step done", func(t *testing.T) {
		m := fsys.NewMemFS()
		m.SetFault(fsys.Fault{At: 0, Kind: fsys.FaultFailAfter})
		if err := m.Mkdir("d"); !errors.Is(err, fsys.ErrInjected) {
			t.Fatalf("error = %v, want ErrInjected", err)
		}
		if _, err := m.ReadDir("d"); err != nil {
			t.Errorf("the directory is not there: %v", err)
		}
		m.SetFault(fsys.Fault{At: 2, Kind: fsys.FaultFailAfter})
		f, err := m.Create("d/f")
		if err != nil {
			t.Fatal(err)
		}
		if _, err := f.Write([]byte("xy")); !errors.Is(err, fsys.ErrInjected) {
			t.Fatalf("write error = %v, want ErrInjected", err)
		}
		if got := files(t, m)["d/f"]; got != "xy" {
			t.Errorf("file = %q, want the write to have happened", got)
		}
	})

	t.Run("a fault fires once", func(t *testing.T) {
		m := fsys.NewMemFS()
		m.SetFault(fsys.Fault{At: 0, Kind: fsys.FaultFail})
		if err := m.Mkdir("d"); !errors.Is(err, fsys.ErrInjected) {
			t.Fatalf("first call = %v, want ErrInjected", err)
		}
		if err := m.Mkdir("d"); err != nil {
			t.Errorf("second call = %v, want success", err)
		}
	})

	t.Run("a fault past the end of the program never fires", func(t *testing.T) {
		m := fsys.NewMemFS()
		m.SetFault(fsys.Fault{At: steps, Kind: fsys.FaultCrash})
		if err := program(m); err != nil {
			t.Errorf("program = %v, want success", err)
		}
	})
}
