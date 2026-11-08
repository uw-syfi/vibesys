package app_test

import (
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// withBuiltTask puts a task whose build step runs build into the repository the tests submit to.
func (w *world) withBuiltTask(build, check, bench []string) {
	tree := tasktest.Scripted("task", check, bench)
	name := task.TasksDir + "/task/" + task.ManifestName
	manifest := tree[name]
	quoted := make([]string, len(build))
	for i, a := range build {
		quoted[i] = strconv.Quote(a)
	}
	manifest.Data = append(manifest.Data, []byte("\n[build]\ncommand = ["+strings.Join(quoted, ", ")+"]\n")...)
	tree[name] = manifest
	w.tasks.Put(string(call.Scope.Repository()), commit, tree)
}

// A failed check or measure job records the stage that ended it, so a reader can tell a candidate
// that did not build from one the check rejected: both leave a nonzero exit code.
func TestAFailedJobRecordsTheStageThatEndedIt(t *testing.T) {
	cases := []struct {
		name     string
		build    []string
		check    []string
		bench    []string
		settings core.Settings
		want     core.JobStage
	}{
		{"a build that fails ends a check job in build", []string{"exit", "5"}, passes, scores, core.CheckSettings{}, core.StageBuild},
		{"a build that fails ends a measure job in build", []string{"exit", "5"}, passes, scores, core.MeasureSettings{Samples: 2}, core.StageBuild},
		{"a check that rejects ends a check job in check", passes, []string{"exit", "3"}, scores, core.CheckSettings{}, core.StageCheck},
		{"a check that rejects ends a measure job in check", passes, []string{"exit", "3"}, scores, core.MeasureSettings{Samples: 2}, core.StageCheck},
		{"a benchmark that fails ends a measure job in measure", passes, passes, []string{"exit", "4"}, core.MeasureSettings{Samples: 2}, core.StageMeasure},
		{"a benchmark with no value ends a measure job in measure", passes, passes, []string{"emit", "junk"}, core.MeasureSettings{Samples: 2}, core.StageMeasure},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			w := newWorld(t)
			w.withBuiltTask(c.build, c.check, c.bench)
			job, _ := done(t, w.newApp(), c.settings, false)
			if job.State != core.JobFailed {
				t.Fatalf("job = %s", describe(job))
			}
			if job.FailedStage != c.want {
				t.Errorf("failed stage = %q, want %q (%s)", job.FailedStage, c.want, describe(job))
			}
		})
	}
}

// A job that passed names no stage.
func TestAJobThatPassedRecordsNoFailedStage(t *testing.T) {
	w := newWorld(t)
	w.withBuiltTask(passes, passes, scores)
	a := w.newApp()
	for _, settings := range []core.Settings{core.CheckSettings{}, core.MeasureSettings{Samples: 2}} {
		job, _ := done(t, a, settings, true)
		if job.State != core.JobSucceeded || job.FailedStage != "" {
			t.Errorf("%s job = %s, failed stage %q", settings.Kind(), describe(job), job.FailedStage)
		}
	}
}
