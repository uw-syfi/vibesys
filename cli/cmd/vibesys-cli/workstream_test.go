package main

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
)

// workstream runs `workstream <sub>` for the test task, with the flags after the subcommand.
func (c *cli) workstream(sub string, args ...string) result {
	c.t.Helper()
	return c.run(append([]string{"workstream", sub, "--task", "kv"}, args...)...)
}

func (c *cli) workstreamOK(sub string, args ...string) result {
	c.t.Helper()
	r := c.workstream(sub, args...)
	if r.code != exitOK {
		c.t.Fatalf("workstream %s %v: exit %d: %s", sub, args, r.code, r.stderr)
	}
	return r
}

func TestAWorkstreamIsStartedFoundEditedAndJoined(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	c.env["VIBESYS_RUN"], c.env["VIBESYS_AGENT"] = "run-7", "lead"

	if got := c.workstreamOK("list").stdout; got != "" {
		t.Fatalf("list on a task with no workstream = %q, want nothing", got)
	}
	if got := c.workstreamOK("start", "  KV   cache layout ").stdout; got != "1\tactive\tKV cache layout\n" {
		t.Fatalf("start = %q", got)
	}
	if got := c.workstreamOK("start", "kv CACHE layout").stdout; got != "1\tactive\tKV cache layout\n" {
		t.Fatalf("start of the same title = %q, want the same workstream", got)
	}
	if got := c.workstreamOK("start", "Batching").stdout; got != "2\tactive\tBatching\n" {
		t.Fatalf("start of another title = %q", got)
	}
	if got := c.workstreamOK("edit", "1", "--status", "paused", "--note", "paused: accuracy gate").stdout; got != "1\tpaused\tKV cache layout\tpaused: accuracy gate\n" {
		t.Fatalf("edit = %q", got)
	}
	if got := c.workstreamOK("list").stdout; got != "1\tpaused\tKV cache layout\tpaused: accuracy gate\n2\tactive\tBatching\n" {
		t.Fatalf("list = %q", got)
	}
	if got := c.workstreamOK("list", "--status", "active").stdout; got != "2\tactive\tBatching\n" {
		t.Fatalf("list --status active = %q", got)
	}
	if got := c.workstreamOK("list", "--status", "paused,closed").stdout; got != "1\tpaused\tKV cache layout\tpaused: accuracy gate\n" {
		t.Fatalf("list --status paused,closed = %q", got)
	}
	if got := c.workstreamOK("edit", "#1", "--note", "").stdout; got != "1\tpaused\tKV cache layout\n" {
		t.Fatalf("an empty --note = %q, want the note cleared", got)
	}

	if got := c.planOK("create", "--title", "page the cache", "--workstream", "1").stdout; got != "1\topen\tpage the cache\tworkstream 1\n" {
		t.Fatalf("plan create --workstream = %q", got)
	}
	if got := c.planOK("create", "--title", "no line yet").stdout; got != "2\topen\tno line yet\n" {
		t.Fatalf("plan create = %q", got)
	}
	if got := c.planOK("edit", "2", "--workstream", "#2").stdout; got != "2\topen\tno line yet\tworkstream 2\n" {
		t.Fatalf("plan edit --workstream = %q", got)
	}
	if got := c.planOK("edit", "2", "--title", "renamed").stdout; got != "2\topen\trenamed\tworkstream 2\n" {
		t.Fatalf("an edit that names no workstream = %q, want the plan left where it was", got)
	}
	var viewed struct {
		View struct {
			Plan struct {
				Workstream  uint32 `json:"workstream"`
				Transitions []struct {
					Status string `json:"status"`
				} `json:"transitions"`
			} `json:"plan"`
		} `json:"view"`
	}
	if err := json.Unmarshal([]byte(c.planOK("view", "1", "--json").stdout), &viewed); err != nil || viewed.View.Plan.Workstream != 1 || len(viewed.View.Plan.Transitions) != 1 {
		t.Fatalf("plan view --json = %+v, %v; want the workstream and the creation as its one transition", viewed, err)
	}
	if view := c.planOK("view", "1").stdout; !strings.Contains(view, "made by lead (run-7)") || !strings.Contains(view, "statuses:\n  open, ") {
		t.Fatalf("plan view lacks who made the plan and its statuses:\n%s", view)
	}
}

// An unknown workstream is NOT_FOUND, which exits 2 and says which flag gave the number, and the
// plan it was for is not made or changed.
func TestAPlanInAnUnknownWorkstreamIsRefusedNamingTheFlag(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	c.workstreamOK("start", "KV cache layout")
	c.planOK("create", "--title", "t")
	for _, n := range []string{"2", "7", "#9"} {
		for _, args := range [][]string{{"create", "--title", "x", "--workstream", n}, {"edit", "1", "--workstream", n}} {
			r := c.plan(args[0], args[1:]...)
			if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, "--workstream") || !strings.Contains(r.stderr, strings.TrimPrefix(n, "#")) {
				t.Errorf("plan %v: exit %d, stdout %q, stderr %q; want exit 2 and --workstream and the number in stderr", args, r.code, r.stdout, r.stderr)
			}
		}
	}
	if got := c.planOK("list").stdout; got != "1\topen\tt\n" {
		t.Fatalf("list = %q, want the one plan, in no workstream", got)
	}
	if r := c.workstream("edit", "9", "--note", "n"); r.code != exitInvalidInput || !strings.Contains(r.stderr, "workstream 9") {
		t.Errorf("edit of an unknown workstream: exit %d, stderr %q", r.code, r.stderr)
	}
}

func TestWorkstreamMistakesAreInvalidInputAndNeverStartTheDaemon(t *testing.T) {
	c := newCLI(t)
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"no subcommand", []string{"workstream"}, "subcommand"},
		{"unknown subcommand", []string{"workstream", "delete"}, "delete"},
		{"no task", []string{"workstream", "list"}, "--task"},
		{"start without a title", []string{"workstream", "start", "--task", "kv"}, "title"},
		{"start with two titles", []string{"workstream", "start", "--task", "kv", "a", "b"}, "one argument"},
		{"list unknown status", []string{"workstream", "list", "--task", "kv", "--status", "done"}, "done"},
		{"list stray argument", []string{"workstream", "list", "--task", "kv", "1"}, "unexpected"},
		{"edit without a number", []string{"workstream", "edit", "--task", "kv", "--note", "n"}, "workstream number"},
		{"edit a non-number", []string{"workstream", "edit", "--task", "kv", "first", "--note", "n"}, "first"},
		{"edit zero", []string{"workstream", "edit", "--task", "kv", "0", "--note", "n"}, "workstream number"},
		{"edit nothing", []string{"workstream", "edit", "--task", "kv", "1"}, "at least one"},
		{"edit unknown status", []string{"workstream", "edit", "--task", "kv", "1", "--status", "done"}, "done"},
		{"plan create in workstream zero", []string{"plan", "create", "--task", "kv", "--title", "t", "--workstream", "0"}, "--workstream"},
		{"plan create in a non-number workstream", []string{"plan", "create", "--task", "kv", "--title", "t", "--workstream", "kv"}, "--workstream"},
		{"plan edit to a negative workstream", []string{"plan", "edit", "--task", "kv", "1", "--workstream", "-1"}, "--workstream"},
	}
	for _, tc := range cases {
		r := c.run(tc.args...)
		if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q; want exit 2, no stdout, %q in stderr", tc.name, r.code, r.stdout, r.stderr, tc.want)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid invocation started a daemon")
	}
}

// What the daemon refuses is reported as it says, with the exit code of its kind.
func TestWorkstreamRefusalsFromTheDaemon(t *testing.T) {
	c := newCLI(t)
	c.taskWithAJob("kv")
	c.workstreamOK("start", "KV cache layout")
	c.workstreamOK("start", "Batching")
	for _, tc := range []struct {
		name string
		args []string
		code int
		want string
	}{
		{"a title of two lines", []string{"start", "a\nb"}, exitInvalidInput, "one line"},
		{"a blank title", []string{"start", "  "}, exitInvalidInput, "title"},
		{"a long title", []string{"start", strings.Repeat("t", 81)}, exitInvalidInput, "title"},
		{"a note of two lines", []string{"edit", "1", "--note", "a\nb"}, exitInvalidInput, "one line"},
		{"a long note", []string{"edit", "1", "--note", strings.Repeat("n", 281)}, exitInvalidInput, "note"},
		{"a title another workstream has", []string{"edit", "2", "--title", "kv  CACHE layout"}, exitFailed, "workstream 1"},
	} {
		r := c.workstream(tc.args[0], tc.args[1:]...)
		if r.code != tc.code || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q; want exit %d, %q in stderr", tc.name, r.code, r.stdout, r.stderr, tc.code, tc.want)
		}
	}
}

// spell writes a title in another case and spacing than the one it was named in: the same name to
// whoever reads it as a name.
func spell(r *rand.Rand, name string) string {
	switch r.IntN(3) {
	case 0:
		name = strings.ToUpper(name)
	case 1:
		name = strings.ToLower(name)
	}
	var b strings.Builder
	b.WriteString(strings.Repeat(" ", r.IntN(3)))
	for _, word := range strings.Fields(name) {
		b.WriteString(word)
		b.WriteString([]string{" ", "  ", "\t", " \t "}[r.IntN(4)])
	}
	return b.String()
}

// Over random sequences of workstream and plan commands, the commands answer as a plain model of
// the rules does: a start finds the workstream that is not closed with the name, or makes the next
// one; an edit changes only what it names, and a reopened workstream whose name was taken since is
// refused; a plan is in the workstream it was given or moved to, and in none the task lacks.
func TestWorkstreamCommandsFollowAModelUnderRandomCalls(t *testing.T) {
	bases := []string{"KV cache layout", "Batching", "Paged attention", "Speculative decoding"}
	for seed := uint64(1); seed <= 8; seed++ {
		r := rand.New(rand.NewPCG(seed, seed*31))
		c := newCLI(t)
		c.taskWithAJob("kv")
		type line struct{ title, status, note string }
		var model []line
		var plans []int // the workstream of plan i+1
		describe := func(n int) string {
			w := model[n-1]
			s := fmt.Sprintf("%d\t%s\t%s", n, w.status, w.title)
			if w.note != "" {
				s += "\t" + w.note
			}
			return s + "\n"
		}
		open := func(title string) int {
			for i, w := range model {
				if w.status != "closed" && strings.EqualFold(w.title, strings.Join(strings.Fields(title), " ")) {
					return i + 1
				}
			}
			return 0
		}
		for step := range 45 {
			where := fmt.Sprintf("seed %d step %d", seed, step)
			switch op := r.IntN(7); {
			case op < 2:
				spelling := spell(r, bases[r.IntN(len(bases))])
				got := c.workstreamOK("start", spelling).stdout
				n := open(spelling)
				if n == 0 {
					model = append(model, line{title: strings.Join(strings.Fields(spelling), " "), status: "active"})
					n = len(model)
				}
				if got != describe(n) {
					t.Fatalf("%s: start %q = %q, want %q", where, spelling, got, describe(n))
				}
			case op == 2 && len(model) > 0:
				n := 1 + r.IntN(len(model))
				status := []string{"active", "paused", "closed"}[r.IntN(3)]
				got := c.workstream("edit", fmt.Sprint(n), "--status", status)
				clash := status != "closed" && model[n-1].status == "closed" && open(model[n-1].title) != 0
				if clash {
					if got.code != exitFailed || got.stdout != "" {
						t.Fatalf("%s: reopening workstream %d onto a name in use = %+v, want exit 1", where, n, got)
					}
					continue
				}
				model[n-1].status = status
				if got.code != exitOK || got.stdout != describe(n) {
					t.Fatalf("%s: edit %d --status %s = %+v, want %q", where, n, status, got, describe(n))
				}
			case op == 3 && len(model) > 0:
				n := 1 + r.IntN(len(model))
				note := []string{"", "paused: accuracy gate", "waiting on the new kernel"}[r.IntN(3)]
				model[n-1].note = note
				if got := c.workstreamOK("edit", fmt.Sprint(n), "--note", note).stdout; got != describe(n) {
					t.Fatalf("%s: edit %d --note %q = %q, want %q", where, n, note, got, describe(n))
				}
			case op == 4:
				ws := r.IntN(len(model) + 3) // 0 is none, and past the last is a workstream the task lacks
				args := []string{"--title", fmt.Sprint("plan ", len(plans)+1)}
				if ws != 0 {
					args = append(args, "--workstream", fmt.Sprint(ws))
				}
				got := c.plan("create", args...)
				if ws > len(model) {
					if got.code != exitInvalidInput || !strings.Contains(got.stderr, "--workstream") {
						t.Fatalf("%s: plan create --workstream %d = %+v, want exit 2 naming --workstream", where, ws, got)
					}
					continue
				}
				plans = append(plans, ws)
				want := fmt.Sprintf("%d\topen\tplan %d", len(plans), len(plans))
				if ws != 0 {
					want += fmt.Sprintf("\tworkstream %d", ws)
				}
				if got.code != exitOK || got.stdout != want+"\n" {
					t.Fatalf("%s: plan create = %+v, want %q", where, got, want)
				}
			case op == 5 && len(plans) > 0:
				p, ws := 1+r.IntN(len(plans)), 1+r.IntN(len(model)+2)
				got := c.plan("edit", fmt.Sprint(p), "--workstream", fmt.Sprint(ws))
				if ws > len(model) {
					if got.code != exitInvalidInput || !strings.Contains(got.stderr, "--workstream") {
						t.Fatalf("%s: plan edit %d --workstream %d = %+v, want exit 2 naming --workstream", where, p, ws, got)
					}
					continue
				}
				plans[p-1] = ws
				if want := fmt.Sprintf("%d\topen\tplan %d\tworkstream %d\n", p, p, ws); got.code != exitOK || got.stdout != want {
					t.Fatalf("%s: plan edit = %+v, want %q", where, got, want)
				}
			default:
				var want strings.Builder
				for n := range model {
					want.WriteString(describe(n + 1))
				}
				if got := c.workstreamOK("list").stdout; got != want.String() {
					t.Fatalf("%s: list = %q, want %q", where, got, want.String())
				}
			}
		}
		var want strings.Builder
		for i, ws := range plans {
			fmt.Fprintf(&want, "%d\topen\tplan %d", i+1, i+1)
			if ws != 0 {
				fmt.Fprintf(&want, "\tworkstream %d", ws)
			}
			want.WriteString("\n")
		}
		if got := c.planOK("list").stdout; got != want.String() {
			t.Fatalf("seed %d: plan list = %q, want %q", seed, got, want.String())
		}
	}
}
