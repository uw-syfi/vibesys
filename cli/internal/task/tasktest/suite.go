package tasktest

import (
	"context"
	"errors"
	"fmt"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// Rig is how a source is made for the suite.
type Rig struct {
	// Make returns a source over a new repository whose one commit has exactly these files, and
	// the repository and the commit. It never reuses a repository, so a commit of one call is not
	// a commit of another.
	Make func(t *testing.T, files task.Tree) (src task.Source, repository string, commit core.Commit)
	// Missing is a full commit that no repository Make returns has.
	Missing core.Commit
}

// Manifest is a valid task manifest, and Objectives its objectives file.
const (
	Manifest = `
version = 1
backends = ["cpu"]

[build]
command = ["make"]
timeout_seconds = 60

[accuracy]
command = ["${PROJECT_ROOT}/.vibesys/tasks/t/check.sh"]
timeout_seconds = 60

[benchmark]
command = ["${PROJECT_ROOT}/.vibesys/tasks/t/bench.sh"]
timeout_seconds = 60

[benchmark.result]
json_argument = "--out"
metric = "ops"
`
	Objectives = `
[[objective]]
name = "ops"
direction = "max"
`
)

// Files returns a repository's files that hold a valid task called name, and one other file.
func Files(name string) task.Tree {
	dir := task.TasksDir + "/" + name + "/"
	return task.Tree{
		dir + task.ManifestName:  {Data: []byte(Manifest)},
		dir + task.ObjectiveName: {Data: []byte(Objectives)},
		dir + "check.sh":         {Data: []byte("#!/bin/sh\nexit 0\n"), Executable: true},
		dir + "bench.sh":         {Data: []byte("#!/bin/sh\n"), Executable: true},
		"src/main.c":             {Data: []byte("int main(void) { return 0; }\n")},
	}
}

func with(files task.Tree, edit func(task.Tree)) task.Tree {
	out := task.Tree{}
	for p, f := range files {
		out[p] = f
	}
	edit(out)
	return out
}

// Run holds the source to the contract.
func Run(t *testing.T, rig Rig) {
	ctx := context.Background()
	version := func(t *testing.T, files task.Tree) string {
		t.Helper()
		src, repo, commit := rig.Make(t, files)
		got, err := src.Task(ctx, repo, commit, "t")
		if err != nil {
			t.Fatalf("Task: %v", err)
		}
		return got.Version
	}
	t.Run("a task is read as the commit has it", func(t *testing.T) {
		src, repo, commit := rig.Make(t, Files("t"))
		got, err := src.Task(ctx, repo, commit, "t")
		if err != nil {
			t.Fatal(err)
		}
		if got.Name != "t" || got.Version == "" || got.Build == nil || got.Build.Argv[0] != "make" || got.Metric.Name != "ops" || got.Metric.Direction != core.Maximize {
			t.Errorf("task = %+v", got)
		}
		if got.Result.JSONArgument != "--out" || got.Check.Resolve("/w")[0] != "/w/.vibesys/tasks/t/check.sh" || got.HasDockerfile {
			t.Errorf("task = %+v", got)
		}
	})
	t.Run("the version names the task's files and nothing else", func(t *testing.T) {
		base := version(t, Files("t"))
		if again := version(t, Files("t")); again != base {
			t.Errorf("the same files are version %s, then %s", base, again)
		}
		other := with(Files("t"), func(f task.Tree) {
			f["src/main.c"] = task.File{Data: []byte("changed")}
			f["README"] = task.File{Data: []byte("x")}
		})
		if v := version(t, other); v != base {
			t.Errorf("files outside the task changed the version: %s, want %s", v, base)
		}
		for name, edit := range map[string]func(task.Tree){
			"a task file's content": func(f task.Tree) {
				f[task.TasksDir+"/t/check.sh"] = task.File{Data: []byte("#!/bin/sh\nexit 1\n"), Executable: true}
			},
			"a task file's executable bit": func(f task.Tree) {
				f[task.TasksDir+"/t/check.sh"] = task.File{Data: []byte("#!/bin/sh\nexit 0\n")}
			},
			"a file added to the task": func(f task.Tree) { f[task.TasksDir+"/t/data.txt"] = task.File{Data: []byte("d")} },
		} {
			if v := version(t, with(Files("t"), edit)); v == base {
				t.Errorf("%s did not change the version", name)
			}
		}
	})
	t.Run("a Dockerfile in the task is seen", func(t *testing.T) {
		src, repo, commit := rig.Make(t, with(Files("t"), func(f task.Tree) { f[task.TasksDir+"/t/Dockerfile"] = task.File{Data: []byte("FROM scratch\n")} }))
		got, err := src.Task(ctx, repo, commit, "t")
		if err != nil || !got.HasDockerfile {
			t.Errorf("task = %+v, %v", got, err)
		}
	})
	t.Run("a task's image keeps a symbolic link that leads to a file of the task", func(t *testing.T) {
		dir := task.TasksDir + "/t/"
		files := with(Files("t"), func(f task.Tree) {
			f[dir+"Dockerfile"] = task.File{Data: []byte("FROM scratch\n")}
			f[dir+"k8s/manifest.yaml"] = task.File{Data: []byte("kind: List\n")}
			f[dir+"k8s/deployed.yaml"] = task.File{Link: "manifest.yaml"}
			f[dir+"conf"] = task.File{Link: "k8s"}
			f[dir+"up/check.sh"] = task.File{Link: "../check.sh"}
		})
		src, repo, commit := rig.Make(t, files)
		got, err := src.Task(ctx, repo, commit, "t")
		if err != nil {
			t.Fatalf("Task: %v", err)
		}
		for rel, want := range map[string]string{"k8s/deployed.yaml": "manifest.yaml", "conf": "k8s", "up/check.sh": "../check.sh"} {
			if f, ok := got.Context[rel]; !ok || f.Link != want || len(f.Data) != 0 {
				t.Errorf("the image's context has %s as %+v (there: %v), want a link to %q", rel, f, ok, want)
			}
		}
		moved := with(files, func(f task.Tree) { f[dir+"k8s/deployed.yaml"] = task.File{Link: "../check.sh"} })
		if v := version(t, moved); v == got.Version {
			t.Error("a link's new target did not change the version")
		}
	})
	t.Run("a task's image refuses a symbolic link that would lead to nothing in it, and says why", func(t *testing.T) {
		dir := task.TasksDir + "/t/"
		// Each case's links, and the link the refusal names with what it says of it.
		for _, c := range []struct {
			name      string
			links     map[string]string
			bad, says string
		}{
			{"absolute", map[string]string{"k8s/deployed.yaml": "/tmp/hotel-k8s-deploy/manifest.yaml"}, "k8s/deployed.yaml", "absolute"},
			{"escaping", map[string]string{"k8s/deployed.yaml": "../../../../src/main.c"}, "k8s/deployed.yaml", "outside the task directory"},
			{"escaping through a link", map[string]string{"k8s/deployed.yaml": "../src/main.c", "src": "../../../src"}, "src", "outside the task directory"},
			{"dangling", map[string]string{"k8s/deployed.yaml": "generated.yaml"}, "k8s/deployed.yaml", "points at nothing"},
			{"a cycle", map[string]string{"a": "b", "b": "a"}, "a", "cycle"},
		} {
			t.Run(c.name, func(t *testing.T) {
				src, repo, commit := rig.Make(t, with(Files("t"), func(f task.Tree) {
					f[dir+"Dockerfile"] = task.File{Data: []byte("FROM scratch\n")}
					for rel, target := range c.links {
						f[dir+rel] = task.File{Link: target}
					}
				}))
				_, err := src.Task(ctx, repo, commit, "t")
				var f *core.Failure
				if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
					t.Fatalf("err = %v, want invalid input", err)
				}
				for _, want := range []string{dir + c.bad, c.says} {
					if !strings.Contains(f.Message, want) {
						t.Errorf("the refusal %q does not say %q", f.Message, want)
					}
				}
			})
		}
	})
	t.Run("a task with no image keeps any symbolic link in its version", func(t *testing.T) {
		dir := task.TasksDir + "/t/"
		linked := with(Files("t"), func(f task.Tree) { f[dir+"deployed.yaml"] = task.File{Link: "/tmp/deployed.yaml"} })
		src, repo, commit := rig.Make(t, linked)
		got, err := src.Task(ctx, repo, commit, "t")
		if err != nil {
			t.Fatalf("a task that runs in its checkout was refused for a link git makes there: %v", err)
		}
		if got.Version == version(t, Files("t")) {
			t.Error("a link did not change the version")
		}
	})
	t.Run("a task or a commit that is not there is not found", func(t *testing.T) {
		src, repo, commit := rig.Make(t, Files("t"))
		for name, call := range map[string]func() error{
			"task":   func() error { _, err := src.Task(ctx, repo, commit, "other"); return err },
			"commit": func() error { _, err := src.Task(ctx, repo, rig.Missing, "t"); return err },
		} {
			var f *core.Failure
			if err := call(); !errors.As(err, &f) || f.Kind != core.FailureNotFound {
				t.Errorf("unknown %s: %v, want not found", name, err)
			}
		}
	})
	t.Run("a task name is a name and not a path", func(t *testing.T) {
		src, repo, commit := rig.Make(t, Files("t"))
		for _, name := range []string{"", "..", "a/b", "../t"} {
			var f *core.Failure
			if _, err := src.Task(ctx, repo, commit, name); !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
				t.Errorf("name %q: %v, want invalid input", name, err)
			}
		}
	})
	t.Run("a task it cannot run is refused and names why", func(t *testing.T) {
		bad := with(Files("t"), func(f task.Tree) {
			f[task.TasksDir+"/t/"+task.ManifestName] = task.File{Data: []byte(strings.Replace(Manifest, "version = 1", "version = 1\nsurprise = true", 1))}
		})
		src, repo, commit := rig.Make(t, bad)
		_, err := src.Task(ctx, repo, commit, "t")
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "surprise") {
			t.Errorf("err = %v, want invalid input naming the key", err)
		}
	})
	t.Run("a cancelled context reads nothing", func(t *testing.T) {
		src, repo, commit := rig.Make(t, Files("t"))
		done, cancel := context.WithCancel(ctx)
		cancel()
		if _, err := src.Task(done, repo, commit, "t"); err == nil {
			t.Error("a cancelled context read a task")
		}
	})
}

// Scripted returns the files of a repository that holds a task called name whose check and
// benchmark are the given commands, which a test writes in the words of a fake environment's
// scripts (`exit 0`, `emit TEXT`) and not as programs. The benchmark reports its result as JSON in
// the field "ops", which is the task's metric, higher is better.
func Scripted(name string, check, bench []string) task.Tree {
	dir := task.TasksDir + "/" + name + "/"
	quote := func(argv []string) string {
		parts := make([]string, len(argv))
		for i, a := range argv {
			parts[i] = strconv.Quote(a)
		}
		return "[" + strings.Join(parts, ", ") + "]"
	}
	manifest := fmt.Sprintf(`
version = 1
backends = ["cpu"]

[accuracy]
command = %s
timeout_seconds = 60

[benchmark]
command = %s
timeout_seconds = 60

[benchmark.result]
json_argument = "--out"
metric = "ops"
`, quote(check), quote(bench))
	return task.Tree{
		dir + task.ManifestName:  {Data: []byte(manifest)},
		dir + task.ObjectiveName: {Data: []byte(Objectives)},
	}
}
