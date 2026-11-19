package app_test

import (
	"bytes"
	"crypto/sha256"
	"encoding/hex"
	"errors"
	"fmt"
	"path"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/app"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// artifactsAt is where the world's jobs keep their artifacts.
func artifactsAt(repository, task string, job core.JobID) string {
	return path.Join("/state", task, string(job))
}

// outputsDir is the artifacts directory of a command job's command, where its outputs are.
func outputsDir(job core.JobID) string {
	return path.Join(artifactsAt("/repo", "task", job), "01-command")
}

func outputsWorld(t *testing.T) *world {
	t.Helper()
	w := newWorld(t)
	w.artifacts = artifactsAt
	return w
}

// submitOutputs submits a command job that declares outputs.
func submitOutputs(t *testing.T, a *app.App, script ...string) core.Job {
	t.Helper()
	spec := command(script...)
	c := spec.Settings.(core.CommandSettings)
	c.Outputs = true
	spec.Settings = c
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	return out.Job
}

// readOutputs reads a job's outputs back, with their content: the job, and each file's bytes.
func readOutputs(t *testing.T, a *app.App, id core.JobID) (core.Job, map[string]string, error) {
	t.Helper()
	var job core.Job
	files := map[string]*bytes.Buffer{}
	err := a.ReadOutputs(ctxFor(t), scope, id, true,
		func(j core.Job) error { job = j; return nil },
		func(c app.OutputChunk) error {
			b := files[c.Path]
			if b == nil {
				b = &bytes.Buffer{}
				files[c.Path] = b
			}
			if uint64(b.Len()) != c.Offset {
				t.Fatalf("a piece of %s at %d after %d bytes", c.Path, c.Offset, b.Len())
			}
			b.Write(c.Data)
			return nil
		})
	out := map[string]string{}
	for p, b := range files {
		out[p] = b.String()
	}
	return job, out, err
}

// failureKind is the kind of the failure err is, or none.
func failureKind(err error) core.FailureKind {
	var f *core.Failure
	if errors.As(err, &f) {
		return f.Kind
	}
	return ""
}

func sha(s string) string {
	sum := sha256.Sum256([]byte(s))
	return hex.EncodeToString(sum[:])
}

// A command that declares outputs keeps what it left in its artifacts directory when it ends, also
// when it fails: each file with its size and digest, in the job beside its commit, environment and
// GPUs, and readable back byte for byte, from another daemon too.
func TestACommandKeepsItsOutputsWhenItEndsHoweverItEnds(t *testing.T) {
	for _, end := range []string{"0", "3"} {
		t.Run("exit "+end, func(t *testing.T) {
			w := outputsWorld(t)
			a := w.newApp()
			job := submitOutputs(t, a, "artifact", "timings.json", `{"m":1}`, ";", "artifact", "shapes/b.csv", "1,2", ";", "artifact", "empty", ";", "exit", end)
			done := awaitFinal(t, a, job.ID)
			want := core.OutputsKept{Files: []core.OutputFile{
				{Path: "empty", Size: 0, SHA256: sha("")},
				{Path: "shapes/b.csv", Size: 3, SHA256: sha("1,2")},
				{Path: "timings.json", Size: 7, SHA256: sha(`{"m":1}`)},
			}}
			if fmt.Sprint(done.Outputs) != fmt.Sprint(want) {
				t.Fatalf("outputs = %+v, want %+v", done.Outputs, want)
			}
			if done.Environment == nil || done.Spec.Commit != commit {
				t.Fatalf("the job's provenance is missing: %+v", done)
			}
			if log := logOf(t, a, job.ID); !strings.Contains(log, "outputs: kept 3 files, 10 bytes") {
				t.Fatalf("the log does not say what was kept: %q", log)
			}
			a.Close()
			again := w.newApp()
			got, files, err := readOutputs(t, again, job.ID)
			if err != nil {
				t.Fatalf("ReadOutputs: %v", err)
			}
			if got.ID != job.ID || fmt.Sprint(got.Outputs) != fmt.Sprint(want) {
				t.Fatalf("read back job %+v", got)
			}
			if files["timings.json"] != `{"m":1}` || files["shapes/b.csv"] != "1,2" || len(files) != 2 {
				t.Fatalf("read back %q (an empty file sends no piece)", files)
			}
		})
	}
}

// A job cancelled while it runs keeps what its command left before the cancel.
func TestACancelledCommandKeepsWhatItLeft(t *testing.T) {
	w := outputsWorld(t)
	a := w.newApp()
	job := submitOutputs(t, a, "artifact", "partial.txt", "half", ";", "mark", "wrote", ";", "hold", "g")
	if !w.gates.Reached(ctxFor(t), "wrote") {
		t.Fatal("the command never wrote")
	}
	if _, err := a.Cancel(ctxFor(t), call, job.ID, "enough"); err != nil {
		t.Fatalf("Cancel: %v", err)
	}
	done := awaitFinal(t, a, job.ID)
	kept, ok := done.Outputs.(core.OutputsKept)
	if done.State != core.JobCancelled || !ok || len(kept.Files) != 1 || kept.Files[0].Path != "partial.txt" {
		t.Fatalf("a cancelled job is %s with outputs %+v", done.State, done.Outputs)
	}
	if log := logOf(t, a, job.ID); !strings.Contains(log, "outputs: kept 1 file, 4 bytes;") {
		t.Fatalf("the log does not say what was kept, in the singular: %q", log)
	}
}

// A job given GPUs of a pool keeps its outputs beside the devices it was given.
func TestOutputsAreKeptBesideTheGPUsTheJobWasGiven(t *testing.T) {
	w, _, _ := poolRig(t)
	w.artifacts = artifactsAt
	a := w.newApp()
	spec := command("artifact", "kernel.json", "{}")
	c := spec.Settings.(core.CommandSettings)
	c.Outputs = true
	spec.Settings = c
	var err error
	if spec.GPUs, err = core.ParseGPUCount(2, "test"); err != nil {
		t.Fatal(err)
	}
	out, err := a.Submit(ctxFor(t), app.SubmitRequest{Context: call, Spec: spec})
	if err != nil {
		t.Fatalf("Submit: %v", err)
	}
	done := awaitFinal(t, a, out.Job.ID)
	if done.GPUs == nil || done.GPUs.Devices.Len() != 2 {
		t.Fatalf("the job's GPUs are %+v, want the two it was given", done.GPUs)
	}
	if kept, ok := done.Outputs.(core.OutputsKept); !ok || len(kept.Files) != 1 {
		t.Fatalf("outputs = %+v", done.Outputs)
	}
}

// What a job cannot keep is refused as a whole, saying why, and reading it back says the same: past
// the bound on files, naming the bound and the count, and a link, naming it.
func TestOutputsPastTheBoundsOrNotRegularAreRefusedByName(t *testing.T) {
	w := outputsWorld(t)
	a := w.newApp()
	var many []string
	for i := range core.MaxOutputFiles + 1 {
		many = append(many, "artifact", fmt.Sprintf("f%04d", i), "x", ";")
	}
	tooMany := submitOutputs(t, a, append(many, "exit", "0")...)
	done := awaitFinal(t, a, tooMany.ID)
	refused, ok := done.Outputs.(core.OutputsRefused)
	if !ok || !strings.Contains(refused.Reason, fmt.Sprintf("%d files", core.MaxOutputFiles+1)) || !strings.Contains(refused.Reason, fmt.Sprint(core.MaxOutputFiles)) {
		t.Fatalf("outputs of %d files = %+v", core.MaxOutputFiles+1, done.Outputs)
	}
	if done.State != core.JobSucceeded {
		t.Fatalf("a refusal of the outputs changed how the command ended: %s", done.State)
	}
	if _, _, err := readOutputs(t, a, tooMany.ID); failureKind(err) != core.FailureConflict || !strings.Contains(err.Error(), refused.Reason) {
		t.Fatalf("reading refused outputs: %v", err)
	}
	if log := logOf(t, a, tooMany.ID); !strings.Contains(log, "outputs: refused: "+refused.Reason) {
		t.Fatalf("the log does not say why: %q", log)
	}

	linked := submitOutputs(t, a, "artifact", "real", "x", ";", "mark", "wrote", ";", "hold", "g")
	if !w.gates.Reached(ctxFor(t), "wrote") {
		t.Fatal("the command never wrote")
	}
	w.files.PutLink(path.Join(outputsDir(linked.ID), "secret"), "/etc/shadow")
	w.gates.Release("g")
	done = awaitFinal(t, a, linked.ID)
	if refused, ok := done.Outputs.(core.OutputsRefused); !ok || !strings.Contains(refused.Reason, `"secret"`) || !strings.Contains(refused.Reason, "symbolic link") {
		t.Fatalf("outputs with a link = %+v", done.Outputs)
	}
}

// A job that keeps no outputs says why: it did not declare them, or has not ended yet.
func TestReadingOutputsAJobDoesNotKeepSaysWhy(t *testing.T) {
	w := outputsWorld(t)
	a := w.newApp()
	plain := submit(t, a, "artifact", "x", "y")
	awaitFinal(t, a, plain.ID)
	if _, _, err := readOutputs(t, a, plain.ID); failureKind(err) != core.FailureInvalidInput || !strings.Contains(err.Error(), "--outputs") {
		t.Fatalf("a job without --outputs: %v", err)
	}
	running := submitOutputs(t, a, "hold", "g")
	awaitState(t, a, running.ID, core.JobRunning)
	if _, _, err := readOutputs(t, a, running.ID); failureKind(err) != core.FailureConflict || !strings.Contains(err.Error(), "running") {
		t.Fatalf("a running job: %v", err)
	}
	w.gates.Release("g")
	awaitFinal(t, a, running.ID)
}

// A kept file that changed since the job ended is refused when it is read, never sent as the job's.
func TestAnOutputThatChangedSinceIsRefused(t *testing.T) {
	w := outputsWorld(t)
	a := w.newApp()
	job := submitOutputs(t, a, "artifact", "r.json", "abc")
	awaitFinal(t, a, job.ID)
	w.files.Put(path.Join(outputsDir(job.ID), "r.json"), []byte("abcdef"))
	if _, _, err := readOutputs(t, a, job.ID); failureKind(err) != core.FailureConflict || !strings.Contains(err.Error(), "changed since") {
		t.Fatalf("a changed file: %v", err)
	}
}
