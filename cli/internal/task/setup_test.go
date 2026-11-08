package task_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

func draftTree(manifest, objectives string, extra ...string) task.Tree {
	tree := task.Tree{
		task.TasksDir + "/t/" + task.ManifestName:  {Data: []byte(manifest)},
		task.TasksDir + "/t/" + task.ObjectiveName: {Data: []byte(objectives)},
	}
	for _, name := range extra {
		tree[task.TasksDir+"/t/"+name] = task.File{Data: []byte(name)}
	}
	return tree
}

const (
	twoObjectives = "[pareto]\nrelative_noise = 0.05\n[[objective]]\nname = \"qps\"\ndirection = \"max\"\nrelative_noise = 0.02\n[[objective]]\nname = \"p99\"\ndirection = \"min\"\nunit = \"ms\"\n"
	oneObjective  = "[[objective]]\nname = \"qps\"\ndirection = \"max\"\n"
)

// What a draft declares is read from a manifest that is not finished: a section counts only with a
// command, and a floor is the objective's own, else the [pareto] one of a task with several.
func TestDraftOfReadsWhatAnUnfinishedManifestDeclares(t *testing.T) {
	cases := []struct {
		name, manifest, objectives string
		check, benchmark           bool
		floors                     []*float64
	}{
		{"nothing yet", "version = \"0.2\"\n", oneObjective, false, false, []*float64{nil}},
		{"accuracy only", "version = \"0.2\"\n[accuracy]\ncommand = [\"true\"]\n", oneObjective, true, false, []*float64{nil}},
		{"check and measure", "version = \"0.2\"\n[check]\ncommand = [\"true\"]\n[measure]\ncommand = [\"true\"]\n", oneObjective, true, true, []*float64{nil}},
		{"a section with no command", "version = \"0.2\"\n[accuracy]\ntimeout_seconds = 5\n[benchmark]\ncommand = []\n", oneObjective, false, false, []*float64{nil}},
		{"floors of a pareto task", "version = \"0.2\"\n[benchmark]\ncommand = [\"true\"]\n", twoObjectives, false, true, []*float64{ptr(0.02), ptr(0.05)}},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			d, err := task.DraftOf(draftTree(c.manifest, c.objectives), "t")
			if err != nil {
				t.Fatal(err)
			}
			decl := d.Declarations
			if decl.GetCheckDeclared() != c.check || decl.GetBenchmarkDeclared() != c.benchmark || len(decl.GetObjectives()) != len(c.floors) || d.Version == "" {
				t.Fatalf("declarations = %v, version %q", decl, d.Version)
			}
			for i, want := range c.floors {
				got := decl.GetObjectives()[i]
				if (want == nil) != (got.NoiseFloor == nil) || (want != nil && *want != got.GetNoiseFloor()) {
					t.Errorf("objective %s floor = %v, want %v", got.GetName(), got.NoiseFloor, want)
				}
			}
		})
	}
}

// The draft's version moves with any file of the directory, not with a file generated there, and
// is the version a job of the same files carries.
func TestDraftOfVersionsTheWholeDirectory(t *testing.T) {
	manifest := "version = \"0.2\"\n[accuracy]\ncommand = [\"true\"]\n"
	version := func(extra ...string) string {
		t.Helper()
		d, err := task.DraftOf(draftTree(manifest, oneObjective, extra...), "t")
		if err != nil {
			t.Fatal(err)
		}
		return d.Version
	}
	if version() != version() || version() == version("helper.sh") || version("helper.sh") == version("other.sh") {
		t.Error("the version does not name exactly the files of the directory")
	}
	if version() != version("__pycache__/x.pyc") {
		t.Error("a generated file changed the version")
	}
}

// A draft that cannot be read says which way: a task that is not there is not found, and a file that
// is invalid is invalid and names the file.
func TestDraftOfRefusesADraftItCannotRead(t *testing.T) {
	_, err := task.DraftOf(task.Tree{}, "t")
	if f := core.AsFailure(err); f.Kind != core.FailureNotFound {
		t.Errorf("no task: %v", err)
	}
	_, err = task.DraftOf(draftTree("version = \"0.2\"\n[accuracy\n", oneObjective), "t")
	if f := core.AsFailure(err); f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, task.ManifestName) {
		t.Errorf("half-written manifest: %v", err)
	}
}

func ptr(f float64) *float64 { return &f }
