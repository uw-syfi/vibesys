package filestore

import (
	"errors"
	"math/rand"
	"slices"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
)

func openMemIndex(t *testing.T, mem *fsys.MemFS) *Index {
	t.Helper()
	x, err := OpenIndex("/state/vibesys", WithFS(mem))
	if err != nil {
		t.Fatal(err)
	}
	return x
}

func TestIndexDir(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	for _, tc := range []struct {
		name string
		env  map[string]string
		want string
	}{
		{"state home set", map[string]string{"XDG_STATE_HOME": "/s"}, "/s/vibesys"},
		{"unset falls back to home", nil, "/home/u/.local/state/vibesys"},
		{"a relative value is not trusted", map[string]string{"XDG_STATE_HOME": "s"}, "/home/u/.local/state/vibesys"},
	} {
		if got := IndexDir(env(tc.env), "/home/u"); got != tc.want {
			t.Errorf("%s: IndexDir = %q, want %q", tc.name, got, tc.want)
		}
	}
}

// The index holds a set: whatever order and however often tasks are added, Tasks returns each
// once, sorted, and a second index over the same disk sees the same.
func TestIndexIsASetThatSurvivesReopening(t *testing.T) {
	rng := rand.New(rand.NewSource(7))
	repos := []string{"/r/a", "/r/b", "/home/u/proj"}
	names := []string{"t1", "t2", "kv"}
	for round := 0; round < 25; round++ {
		mem := fsys.NewMemFS()
		x := openMemIndex(t, mem)
		want := map[Task]bool{}
		for i := rng.Intn(12); i >= 0; i-- {
			task := Task{Repository: repos[rng.Intn(len(repos))], Name: names[rng.Intn(len(names))]}
			if err := x.Add(task); err != nil {
				t.Fatal(err)
			}
			want[task] = true
		}
		for _, index := range []*Index{x, openMemIndex(t, mem)} {
			got, err := index.Tasks()
			if err != nil {
				t.Fatal(err)
			}
			if len(got) != len(want) || !slices.IsSortedFunc(got, func(a, b Task) int {
				if a.Repository != b.Repository {
					return map[bool]int{true: -1, false: 1}[a.Repository < b.Repository]
				}
				return map[bool]int{true: -1, false: 1}[a.Name < b.Name]
			}) {
				t.Fatalf("Tasks = %v, want the %d added, sorted", got, len(want))
			}
			for _, task := range got {
				if !want[task] {
					t.Errorf("Tasks has %v, which was never added", task)
				}
			}
		}
	}
}

func TestIndexRefusesWhatItCannotName(t *testing.T) {
	x := openMemIndex(t, fsys.NewMemFS())
	for _, task := range []Task{{"relative/repo", "t"}, {"/r", ""}, {"/r", ".hidden"}, {"/r", "a/b"}} {
		var f *core.Failure
		if err := x.Add(task); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("Add(%v) = %v, want invalid input", task, err)
		}
	}
}

// A machine that dies while a task is added leaves the index as it was or with the task in it,
// never a damaged entry.
func TestIndexAddIsAtomicAtEveryStep(t *testing.T) {
	probe := fsys.NewMemFS()
	x := openMemIndex(t, probe)
	before := probe.Steps()
	if err := x.Add(Task{"/r", "t"}); err != nil {
		t.Fatal(err)
	}
	steps := probe.Steps() - before
	for step := 0; step < steps; step++ {
		for _, kind := range []fsys.FaultKind{fsys.FaultCrash, fsys.FaultTornWrite} {
			power := kind == fsys.FaultTornWrite
			mem := fsys.NewMemFS()
			index := openMemIndex(t, mem)
			base := mem.Steps()
			mem.SetFault(fsys.Fault{At: base + step, Kind: kind})
			_ = index.Add(Task{"/r", "t"})
			after, err := OpenIndex("/state/vibesys", WithFS(mem.CrashImage(power)))
			if err != nil {
				t.Fatalf("step %d: reopening: %v", step, err)
			}
			got, err := after.Tasks()
			if err != nil || len(got) > 1 {
				t.Fatalf("step %d power=%v: Tasks = %v, %v; want none or the one added", step, power, got, err)
			}
		}
	}
}

func TestIndexNamesAnEntryItCannotRead(t *testing.T) {
	mem := fsys.NewMemFS()
	x := openMemIndex(t, mem)
	if err := x.Add(Task{"/r", "t"}); err != nil {
		t.Fatal(err)
	}
	for name := range mem.Files() {
		if len(name) > 5 && name[len(name)-5:] == ".json" {
			mem.Put(name, []byte(`{"format":99,"repository":"/r","task":"t"}`))
		}
	}
	_, err := x.Tasks()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureIncompatible {
		t.Fatalf("Tasks = %v, want an incompatible-format failure naming the file", err)
	}
}
