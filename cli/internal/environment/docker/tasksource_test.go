package docker_test

import (
	"context"
	"errors"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

const repository = "/repo"

const dockerfilePath = ".vibesys/tasks/t/Dockerfile"

// taskTree is a repository with the task "t", and the files added to it.
func taskTree(extra map[string]string) task.Tree {
	tree := tasktest.Files("t")
	for name, text := range extra {
		tree[name] = task.File{Data: []byte(text)}
	}
	return tree
}

func sourceWith(commit core.Commit, tree task.Tree) docker.TaskSource {
	m := task.NewMemorySource()
	m.Put(repository, commit, tree)
	return docker.TaskSource{Tasks: m}
}

func TestTheSpecIsTheDockerfileOfTheTasksCommit(t *testing.T) {
	src := sourceWith("c1", taskTree(map[string]string{dockerfilePath: "FROM scratch\n"}))
	spec, err := src.Spec(context.Background(), environment.Target{Repository: repository, Commit: "c1", Task: "t"})
	if err != nil || string(spec.Dockerfile) != "FROM scratch\n" || spec.NestedDocker {
		t.Fatalf("Spec = %+v, %v", spec, err)
	}
}

func TestATaskWithoutADockerfileIsRefusedWithTheWayOut(t *testing.T) {
	src := sourceWith("c1", taskTree(nil))
	_, err := src.Spec(context.Background(), environment.Target{Repository: repository, Commit: "c1", Task: "t"})
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureNotFound || !strings.Contains(f.Message, "--env host") {
		t.Fatalf("err = %v, want a not-found failure that names --env host", err)
	}
}

func TestAnUnknownCommitOrTaskKeepsTheSourcesFailure(t *testing.T) {
	src := sourceWith("c1", taskTree(map[string]string{dockerfilePath: "FROM scratch\n"}))
	for name, target := range map[string]environment.Target{
		"commit": {Repository: repository, Commit: "other", Task: "t"},
		"task":   {Repository: repository, Commit: "c1", Task: "missing"},
	} {
		_, err := src.Spec(context.Background(), target)
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureNotFound {
			t.Errorf("unknown %s: err = %v, want a not-found failure", name, err)
		}
	}
}

func TestADockerInDockerTaskIsPassedOnToBeRefused(t *testing.T) {
	tree := taskTree(map[string]string{dockerfilePath: "FROM scratch\n"})
	tree[".vibesys/tasks/t/"+task.ManifestName] = task.File{Data: []byte(tasktest.Manifest + "\n[environment]\ndocker_in_docker = true\n")}
	spec, err := sourceWith("c1", tree).Spec(context.Background(), environment.Target{Repository: repository, Commit: "c1", Task: "t"})
	if err != nil || !spec.NestedDocker {
		t.Fatalf("Spec = %+v, %v, want NestedDocker", spec, err)
	}
}
