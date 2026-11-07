package main

import (
	"encoding/json"
	"math/rand/v2"
	"strconv"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// servingFiles is a task called "kv" that scores throughput and reports a latency beside it.
func servingFiles() task.Tree {
	files := tasktest.Files("kv")
	dir := task.TasksDir + "/kv/"
	files[dir+task.ManifestName] = task.File{Data: []byte("version = \"0.1\"\n\n[agent]\ndomain = \"kernels\"\n\n[check]\ncommand = [\"./check\"]\n\n[measure]\ncommand = [\"./bench\"]\nresult_protocol = 2\n")}
	files[dir+task.ObjectiveName] = task.File{Data: []byte("[[objective]]\nname = \"tok_s\"\ndirection = \"max\"\n\n[[objective]]\nname = \"ttft_ms\"\ndirection = \"min\"\n\n[pareto]\nrelative_noise = 0.02\n")}
	return files
}

func (c *cli) bound(sub string, args ...string) result {
	c.t.Helper()
	return c.run(append([]string{"bound", sub, "--task", "kv"}, args...)...)
}

func (c *cli) boundOK(sub string, args ...string) result {
	c.t.Helper()
	r := c.bound(sub, args...)
	if r.code != exitOK {
		c.t.Fatalf("bound %s %v: exit %d: %s", sub, args, r.code, r.stderr)
	}
	return r
}

type boundsJSON struct {
	Bounds []struct {
		Seq       string  `json:"seq"`
		Objective string  `json:"objective"`
		Direction string  `json:"direction"`
		Value     float64 `json:"value"`
		Unit      string  `json:"unit"`
		Note      string  `json:"note"`
		Origin    struct {
			RunID string `json:"run_id"`
			Agent string `json:"agent"`
			At    string `json:"at"`
		} `json:"origin"`
	} `json:"bounds"`
}

func (c *cli) boundsJSON(args ...string) boundsJSON {
	c.t.Helper()
	var out boundsJSON
	r := c.boundOK("list", append(args, "--json")...)
	if err := json.Unmarshal([]byte(r.stdout), &out); err != nil {
		c.t.Fatalf("bound list --json: %q is not JSON: %v", r.stdout, err)
	}
	return out
}

// An agent declares a bound early, before any job has run, and revises it as it learns. Every
// version stays, each says who declared it, and the list marks the one that stands.
func TestABoundIsDeclaredRevisedAndKeptInAnAppendOnlyLog(t *testing.T) {
	c := newCLI(t)
	c.world.Tasks.Put("/repo", full, servingFiles())
	c.env["VIBESYS_RUN"], c.env["VIBESYS_AGENT"] = "run-7", "lead"

	if got := c.bound("list"); got.code != exitOK || got.stdout != "" {
		t.Fatalf("list on a task with no store = %d %q, want nothing", got.code, got.stdout)
	}
	if r := c.boundOK("declare", "--objective", "tok_s", "--value", "15600", "--unit", "tok/s", "--note", "bandwidth over bytes per token"); r.stdout != "1\ttok_s\t15600 tok/s\tmaximize\n" {
		t.Fatalf("declare = %q", r.stdout)
	}
	if r := c.boundOK("declare", "--objective", "ttft_ms", "--value", "74", "--unit", "ms", "--note", "prefill flops over peak"); r.stdout != "2\tttft_ms\t74 ms\tminimize\n" {
		t.Fatalf("declare = %q", r.stdout)
	}
	c.env["VIBESYS_AGENT"] = "planner"
	if r := c.boundOK("declare", "--objective", "tok_s", "--value", "12000.5", "--unit", "tok/s", "--note", "the KV cache does not fit at full batch"); r.stdout != "3\ttok_s\t12000.5 tok/s\tmaximize\n" {
		t.Fatalf("declare = %q", r.stdout)
	}

	list := strings.Split(strings.TrimSuffix(c.boundOK("list").stdout, "\n"), "\n")
	want := []struct{ prefix, state, note string }{
		{"1\ttok_s\t15600 tok/s\tmaximize\t", "lead (run-7)\tsuperseded", "bandwidth over bytes per token"},
		{"2\tttft_ms\t74 ms\tminimize\t", "lead (run-7)\tcurrent", "prefill flops over peak"},
		{"3\ttok_s\t12000.5 tok/s\tmaximize\t", "planner (run-7)\tcurrent", "the KV cache does not fit at full batch"},
	}
	if len(list) != 2*len(want) {
		t.Fatalf("list = %q, want a line and a note for each of three bounds", list)
	}
	for i, w := range want {
		line, note := list[2*i], list[2*i+1]
		if !strings.HasPrefix(line, w.prefix) || !strings.HasSuffix(line, w.state) || note != "    "+w.note {
			t.Errorf("entry %d = %q / %q, want %q ... %q and the note %q", i, line, note, w.prefix, w.state, w.note)
		}
	}
	if only := c.boundOK("list", "--objective", "ttft_ms").stdout; strings.Count(only, "\n") != 2 || !strings.HasPrefix(only, "2\tttft_ms") {
		t.Errorf("list --objective ttft_ms = %q", only)
	}

	got := c.boundsJSON()
	if len(got.Bounds) != 3 || got.Bounds[0].Seq != "1" || got.Bounds[0].Value != 15600 || got.Bounds[0].Direction != "DIRECTION_MAXIMIZE" ||
		got.Bounds[1].Direction != "DIRECTION_MINIMIZE" || got.Bounds[0].Origin.RunID != "run-7" || got.Bounds[0].Origin.Agent != "lead" ||
		got.Bounds[2].Origin.Agent != "planner" || got.Bounds[0].Origin.At == "" || got.Bounds[0].Note != "bandwidth over bytes per token" {
		t.Fatalf("list --json = %+v", got)
	}
	if only := c.boundsJSON("--objective", "tok_s"); len(only.Bounds) != 2 || only.Bounds[1].Seq != "3" {
		t.Fatalf("list --objective tok_s --json = %+v", only)
	}
}

// An objective the task does not declare is refused, naming it and the ones that are declared, and
// nothing is recorded.
func TestABoundForAnUnknownObjectiveIsRefusedNamingIt(t *testing.T) {
	c := newCLI(t)
	c.world.Tasks.Put("/repo", full, servingFiles())
	r := c.bound("declare", "--objective", "goodput", "--value", "10", "--unit", "x", "--note", "n")
	if r.code != exitInvalidInput || !strings.Contains(r.stderr, `"goodput"`) || !strings.Contains(r.stderr, `"tok_s"`) || !strings.Contains(r.stderr, `"ttft_ms"`) {
		t.Fatalf("declare = exit %d, stderr %q; want invalid input naming goodput and the declared objectives", r.code, r.stderr)
	}
	if got := c.bound("declare", "--objective", "goodput", "--value", "10", "--unit", "x", "--note", "n", "--json"); got.code != exitInvalidInput || !strings.Contains(got.stderr, "goodput") {
		t.Fatalf("declare --json = exit %d, stderr %q", got.code, got.stderr)
	}
	if got := c.boundOK("list"); got.stdout != "" {
		t.Fatalf("list = %q after a refused declaration, want nothing", got.stdout)
	}
}

func TestABoundNeedsItsFlagsAndAValueAboveZero(t *testing.T) {
	c := newCLI(t)
	c.world.Tasks.Put("/repo", full, servingFiles())
	good := []string{"--objective", "tok_s", "--value", "10", "--unit", "tok/s", "--note", "n"}
	without := func(flag string) []string {
		var out []string
		for i := 0; i < len(good); i += 2 {
			if good[i] != flag {
				out = append(out, good[i], good[i+1])
			}
		}
		return out
	}
	for _, flag := range []string{"--objective", "--value", "--unit", "--note"} {
		r := c.bound("declare", without(flag)...)
		if r.code != exitInvalidInput || !strings.Contains(r.stderr, flag) {
			t.Errorf("declare without %s = exit %d, stderr %q; want invalid input naming it", flag, r.code, r.stderr)
		}
	}
	for _, v := range []string{"0", "-1", "NaN", "Inf", "abc", "1e999", " "} {
		r := c.bound("declare", "--objective", "tok_s", "--value", v, "--unit", "u", "--note", "n")
		if r.code != exitInvalidInput || !strings.Contains(r.stderr, "--value") {
			t.Errorf("--value %q = exit %d, stderr %q; want invalid input naming --value", v, r.code, r.stderr)
		}
	}
	if r := c.bound("nonsense"); r.code != exitInvalidInput || !strings.Contains(r.stderr, "nonsense") {
		t.Errorf("an unknown subcommand = exit %d, stderr %q", r.code, r.stderr)
	}
	if r := c.run("bound"); r.code != exitInvalidInput {
		t.Errorf("bound with no subcommand = exit %d", r.code)
	}
	if got := c.boundOK("list"); got.stdout != "" {
		t.Fatalf("list = %q after refused declarations, want nothing", got.stdout)
	}
}

// The note takes the three forms a plan's text does: a flag, standard input and a file.
func TestTheNoteOfABoundCanComeFromStandardInputOrAFile(t *testing.T) {
	c := newCLI(t)
	c.world.Tasks.Put("/repo", full, servingFiles())
	c.stdin = "derived on stdin\nover two lines\n"
	c.boundOK("declare", "--objective", "tok_s", "--value", "10", "--unit", "tok/s", "--note", "-")
	c.files["note.md"] = "derived in a file"
	c.boundOK("declare", "--objective", "tok_s", "--value", "11", "--unit", "tok/s", "--note-file", "note.md")
	got := c.boundsJSON()
	if len(got.Bounds) != 2 || got.Bounds[0].Note != "derived on stdin\nover two lines\n" || got.Bounds[1].Note != "derived in a file" {
		t.Fatalf("notes = %+v", got)
	}
	if r := c.bound("declare", "--objective", "tok_s", "--value", "1", "--unit", "u", "--note", "a", "--note-file", "note.md"); r.code != exitInvalidInput {
		t.Errorf("a note given two ways = exit %d, want invalid input", r.code)
	}
}

// Whatever order bounds are declared in, the list holds each one once, in the order declared, with
// exactly the newest of each objective marked current.
func TestTheListMarksTheNewestBoundOfEachObjectiveCurrent(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	c := newCLI(t)
	c.world.Tasks.Put("/repo", full, servingFiles())
	objectives := []string{"tok_s", "ttft_ms"}
	var order []string
	newest := map[string]int{}
	for i := 1; i <= 10; i++ {
		o := objectives[r.IntN(2)]
		c.boundOK("declare", "--objective", o, "--value", "5", "--unit", "u", "--note", "n")
		order = append(order, o)
		newest[o] = i
	}
	lines := strings.Split(strings.TrimSuffix(c.boundOK("list").stdout, "\n"), "\n")
	current := map[string]int{}
	for i := 0; i < len(lines); i += 2 {
		fields := strings.Split(lines[i], "\t")
		place := i/2 + 1
		if len(fields) != 7 || fields[0] != strconv.Itoa(place) || fields[1] != order[place-1] {
			t.Fatalf("line %d = %q, want place %d for %s", i, lines[i], place, order[place-1])
		}
		if (fields[6] == "current") != (newest[fields[1]] == place) {
			t.Errorf("place %d of %s is marked %q, and the newest is %d", place, fields[1], fields[6], newest[fields[1]])
		}
		if fields[6] == "current" {
			current[fields[1]]++
		}
	}
	for o := range newest {
		if current[o] != 1 {
			t.Errorf("%s has %d current bounds, want 1", o, current[o])
		}
	}
}
