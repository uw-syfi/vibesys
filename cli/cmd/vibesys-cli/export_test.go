package main

import (
	"bytes"
	"encoding/json"
	"io/fs"
	"maps"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// tree is every regular file under dir, by slash-separated path, with its bytes.
func tree(t *testing.T, dir string) map[string]string {
	t.Helper()
	out := map[string]string{}
	err := filepath.WalkDir(dir, func(at string, e fs.DirEntry, err error) error {
		if err != nil || e.IsDir() {
			return err
		}
		raw, err := os.ReadFile(at)
		if err != nil {
			return err
		}
		rel, _ := filepath.Rel(dir, at)
		out[filepath.ToSlash(rel)] = string(raw)
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return out
}

// export runs export into dir, which must succeed, and returns the index it printed.
func (c *cli) export(dir string, args ...string) *cliv1.ExportIndex {
	c.t.Helper()
	r := c.run(append([]string{"export", dir, "--task", "kv", "--json"}, args...)...)
	if r.code != exitOK {
		c.t.Fatalf("export %v: exit %d: %s", args, r.code, r.stderr)
	}
	var index cliv1.ExportIndex
	if err := protojson.Unmarshal([]byte(r.stdout), &index); err != nil {
		c.t.Fatalf("export --json: %q: %v", r.stdout, err)
	}
	return &index
}

// stdoutOf runs a command that must succeed and returns what it printed.
func (c *cli) stdoutOf(args ...string) string {
	c.t.Helper()
	r := c.run(args...)
	if r.code != exitOK {
		c.t.Fatalf("%v: exit %d: %s", args, r.code, r.stderr)
	}
	return r.stdout
}

// sameFiles fails unless the index lists exactly the files of the tree but index.json itself.
func sameFiles(t *testing.T, index *cliv1.ExportIndex, files map[string]string) {
	t.Helper()
	var listed []string
	for _, f := range index.GetEntries() {
		listed = append(listed, f.GetName())
	}
	want := slices.Sorted(maps.Keys(files))
	want = slices.DeleteFunc(want, func(p string) bool { return p == "index.json" })
	if !slices.Equal(listed, want) {
		t.Fatalf("the index lists %v, the directory holds %v", listed, want)
	}
}

// A task with plans, comments, a status history, jobs with notes and logs, a measured commit, a bound
// and a workstream exports to the same bytes every time, into a new directory or over an old export.
// Each text file is what its command prints, and each JSON file is its command's --json answer.
func TestAnExportIsTheSameBytesEveryTime(t *testing.T) {
	c := newResultsCLI(t)
	c.env["VIBESYS_AGENT"] = "lead"
	c.stdoutOf("workstream", "start", "--task", "kv", "KV cache layout")
	c.planOK("create", "--title", "shard the index", "--body", "# Why\n\nless contention", "--workstream", "1")
	c.planOK("create", "--title", "inline the hot path")
	c.planOK("comment", "1", "--body", "implementing in worktree a")
	c.planOK("close", "2", "--status", "rejected", "--comment", "no gain")
	c.stdoutOf("bound", "declare", "--task", "kv", "--objective", "ops", "--value", "200", "--unit", "ops/s", "--note", "peak", "--commit", "c1")
	storetest.SeedCheck(t, c.store, rev(1), c.clk.Now())
	c.measured(1, win)
	job := c.submit("print", "hello", ";", "print", "world", ";", "exit", "0")
	c.stdoutOf(withTask("wait", job)...)
	c.stdoutOf(withTask("note", job, "the first try")...)

	a, b := t.TempDir(), t.TempDir()
	first := c.export(a)
	files := tree(t, a)
	sameFiles(t, first, files)
	c.export(b)
	if again := tree(t, b); !maps.Equal(files, again) {
		t.Fatalf("a second export differs from the first")
	}
	c.export(a)
	if again := tree(t, a); !maps.Equal(files, again) {
		t.Fatalf("exporting over an export changed it")
	}

	commit := string(rev(1))
	for name, args := range map[string][]string{
		"plans.txt":                 []string{"plan", "list", "--task", "kv"},
		"plan/0001.txt":             []string{"plan", "view", "1", "--task", "kv"},
		"plan/0002.txt":             []string{"plan", "view", "2", "--task", "kv"},
		"jobs.txt":                  withTask("jobs"),
		"log/" + job + ".log":       withTask("logs", job),
		"commit/" + commit + ".txt": withTask("show", commit),
	} {
		if got, want := files[name], c.stdoutOf(args...); got != want {
			t.Errorf("%s = %q, want what %v prints, %q", name, got, args, want)
		}
	}
	for name, args := range map[string][]string{
		"plan/0002.json":             []string{"plan", "view", "2", "--json", "--task", "kv"},
		"jobs.json":                  withTask("jobs", "--json"),
		"commit/" + commit + ".json": withTask("show", commit, "--json"),
		"bounds.json":                []string{"bound", "list", "--json", "--task", "kv"},
		"workstreams.json":           []string{"workstream", "list", "--json", "--task", "kv"},
	} {
		if got, want := compacted(t, files[name]), c.stdoutOf(args...); got != want {
			t.Errorf("%s = %q, want what %v prints, %q", name, got, args, want)
		}
	}
	if !strings.Contains(files["plan/0002.txt"], "rejected") || !strings.Contains(files["plan/0001.txt"], "implementing in worktree a") ||
		!strings.Contains(files["jobs.txt"], "note: the first try") || files["log/"+job+".log"] != "hello\nworld\n" {
		t.Errorf("the export lost a comment, a status, a note or a log: %v", files)
	}
}

// compacted is an exported JSON file as --json prints it, on one line.
func compacted(t *testing.T, text string) string {
	t.Helper()
	var b bytes.Buffer
	if err := json.Compact(&b, []byte(text)); err != nil {
		t.Fatalf("%q is not JSON: %v", text, err)
	}
	return b.String() + "\n"
}

// With a run, only the jobs a caller of that run submitted are exported, and the plans the run made
// or linked a job to.
func TestAnExportOfARunHoldsOnlyItsJobsAndPlans(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_RUN"] = "r1"
	mine := c.submit("print", "mine", ";", "exit", "0")
	c.planOK("create", "--title", "the run's plan")
	c.env["VIBESYS_RUN"] = "r2"
	theirs := c.submit("print", "theirs", ";", "exit", "0")
	c.planOK("create", "--title", "another run's plan")
	c.stdoutOf(withTask("wait", mine, theirs)...)

	dir := t.TempDir()
	index := c.export(dir, "--run", "r1")
	files := tree(t, dir)
	sameFiles(t, index, files)
	if index.GetRunId() != "r1" || files["log/"+mine+".log"] != "mine\n" || files["log/"+theirs+".log"] != "" ||
		!strings.Contains(files["plans.txt"], "the run's plan") || strings.Contains(files["plans.txt"], "another") ||
		strings.Contains(files["jobs.txt"], theirs) {
		t.Fatalf("export --run r1 = %v", files)
	}
	all := c.export(t.TempDir(), "--run", "")
	if got := len(slices.DeleteFunc(all.GetEntries(), func(f *cliv1.ExportEntry) bool {
		return f.GetContent() != cliv1.ExportContent_EXPORT_CONTENT_JOB_LOG
	})); got != 2 {
		t.Fatalf("export of every run wrote %d logs, want 2", got)
	}
}

// A log over --max-log-bytes keeps its first half and its last half of the limit, with a marker
// between, whatever the limit; one at most the limit is kept whole. The index says how long the
// output was and how much was left out.
func TestALongLogKeepsItsHeadAndTail(t *testing.T) {
	c := newCLI(t)
	var argv []string
	var log strings.Builder
	for i := range 40 {
		line := "line " + strconv.Itoa(i)
		argv = append(argv, "print", line, ";")
		log.WriteString(line + "\n")
	}
	job := c.submit(append(argv, "exit", "0")...)
	c.stdoutOf(withTask("wait", job)...)
	whole := log.String()
	rng := rand.New(rand.NewPCG(7, 11))
	limits := []uint64{1, 2, uint64(len(whole)) - 1, uint64(len(whole)), uint64(len(whole)) + 1}
	for range 12 {
		limits = append(limits, 1+rng.Uint64N(uint64(len(whole))+20))
	}
	for _, limit := range limits {
		dir := t.TempDir()
		index := c.export(dir, "--max-log-bytes", strconv.FormatUint(limit, 10))
		got := tree(t, dir)["log/"+job+".log"]
		i := slices.IndexFunc(index.GetEntries(), func(f *cliv1.ExportEntry) bool { return f.GetSubject() == job })
		entry := index.GetEntries()[i]
		total := uint64(len(whole))
		if entry.GetLogBytes() != total {
			t.Fatalf("limit %d: the index says the log held %d bytes, want %d", limit, entry.GetLogBytes(), total)
		}
		if total <= limit {
			if got != whole || entry.GetOmittedBytes() != 0 {
				t.Fatalf("limit %d: a log within it was cut: %q, %d omitted", limit, got, entry.GetOmittedBytes())
			}
			continue
		}
		head, tail := whole[:limit/2], whole[total-(limit-limit/2):]
		marker := "\n[vibesys-cli export: " + strconv.FormatUint(total-limit, 10) + " of " + strconv.FormatUint(total, 10) + " bytes left out here"
		if !strings.HasPrefix(got, head+marker) || !strings.HasSuffix(got, "]\n"+tail) || entry.GetOmittedBytes() != total-limit {
			t.Fatalf("limit %d: kept %q, %d omitted; want head %q and tail %q", limit, got, entry.GetOmittedBytes(), head, tail)
		}
	}
}

// A task with nothing in it, not even a store, exports to empty lists and an index.
func TestAnEmptyTaskExportsCleanly(t *testing.T) {
	c := newCLI(t)
	dir := filepath.Join(t.TempDir(), "made", "here")
	index := c.export(dir)
	files := tree(t, dir)
	sameFiles(t, index, files)
	if files["plans.txt"] != "" || files["jobs.txt"] != "" || files["jobs.json"] != "{}\n" || !strings.Contains(files["README.md"], "task kv") {
		t.Fatalf("export of an empty task = %v", files)
	}
	if r := c.run("export", dir, "--task", "kv"); r.code != exitOK || r.stdout != "exported 0 plans, 0 jobs, 0 measured commits to "+dir+"\n" {
		t.Fatalf("export = %+v", r)
	}
}

// --outputs copies the files a command job kept beside its log, as outputs --to does; without it
// none is copied.
func TestAnExportCopiesKeptOutputsOnlyWhenAsked(t *testing.T) {
	c := newOutputsCLI(t)
	r := c.run(withTask("submit", "command", "--image", "harness", "--env", "host", "--outputs", "--wait", "HEAD", "--",
		"artifact", "timings.json", `{"ms":[1,2]}`, ";", "exit", "0")...)
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
	job := strings.Fields(r.stdout)[0]
	without := t.TempDir()
	c.export(without)
	if _, err := os.Stat(filepath.Join(without, "outputs")); !os.IsNotExist(err) {
		t.Fatalf("an export without --outputs copied outputs: %v", err)
	}
	with := t.TempDir()
	index := c.export(with, "--outputs")
	if got := tree(t, with)["outputs/"+job+"/timings.json"]; got != `{"ms":[1,2]}` || !index.GetOutputs() {
		t.Fatalf("outputs/%s/timings.json = %q", job, got)
	}
}

// A mistake in the arguments is refused before the daemon is reached.
func TestExportRefusesAMissingDirectory(t *testing.T) {
	c := newCLI(t)
	if r := c.run("export", "--task", "kv"); r.code != exitInvalidInput || !strings.Contains(r.stderr, "directory") {
		t.Fatalf("export with no directory = %+v", r)
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid export started a daemon")
	}
}
