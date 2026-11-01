package main

import (
	"encoding/json"
	"fmt"
	"math/rand/v2"
	"strings"
	"testing"
)

// plan runs `plan <sub>` for the test task, with the flags after the subcommand.
func (c *cli) plan(sub string, args ...string) result {
	c.t.Helper()
	return c.run(append([]string{"plan", sub, "--task", "kv"}, args...)...)
}

// planOK runs a plan subcommand that must succeed.
func (c *cli) planOK(sub string, args ...string) result {
	c.t.Helper()
	r := c.plan(sub, args...)
	if r.code != exitOK {
		c.t.Fatalf("plan %s %v: exit %d: %s", sub, args, r.code, r.stderr)
	}
	return r
}

type planJSON struct {
	Number   uint32 `json:"number"`
	Title    string `json:"title"`
	Body     string `json:"body"`
	Status   string `json:"status"`
	Comments []struct {
		Text  string `json:"text"`
		Agent string `json:"agent"`
	} `json:"comments"`
}

func (c *cli) planJSON(sub string, args ...string) planJSON {
	c.t.Helper()
	r := c.planOK(sub, append(args, "--json")...)
	var out struct {
		Plan planJSON `json:"plan"`
	}
	if err := json.Unmarshal([]byte(r.stdout), &out); err != nil {
		c.t.Fatalf("plan %s --json: %q is not JSON: %v", sub, r.stdout, err)
	}
	return out.Plan
}

func TestAPlanGoesFromCreatedToClosed(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_AGENT"] = "lead"

	r := c.planOK("create", "--title", "shard the index", "--body", "# Why\n\nless contention")
	if r.stdout != "1\topen\tshard the index\n" {
		t.Fatalf("create = %q", r.stdout)
	}
	c.planOK("create", "--title", "inline the hot path")

	if r := c.planOK("edit", "1", "--title", "shard by key", "--status", "chosen"); r.stdout != "1\tchosen\tshard by key\n" {
		t.Fatalf("edit = %q", r.stdout)
	}
	c.planOK("comment", "#1", "--body", "implementing in worktree a")
	r = c.planOK("close", "1", "--status", "won", "--comment", "1.8x on the benchmark")
	if r.stdout != "1\twon\tshard by key\n" {
		t.Fatalf("close = %q", r.stdout)
	}

	view := c.planOK("view", "1").stdout
	for _, want := range []string{"1\twon\tshard by key", "# Why", "less contention", "comments (2):", "lead,", "implementing in worktree a", "1.8x on the benchmark"} {
		if !strings.Contains(view, want) {
			t.Errorf("view lacks %q:\n%s", want, view)
		}
	}
	if got := c.planOK("list").stdout; got != "1\twon\tshard by key\n2\topen\tinline the hot path\n" {
		t.Fatalf("list = %q", got)
	}
	if got := c.planOK("list", "--status", "open").stdout; got != "2\topen\tinline the hot path\n" {
		t.Fatalf("list --status open = %q", got)
	}
	if got := c.planOK("list", "--status", "won,rejected").stdout; got != "1\twon\tshard by key\n" {
		t.Fatalf("list --status won,rejected = %q", got)
	}
	if got := c.planOK("list", "--status", "won", "--status", "open").stdout; strings.Count(got, "\n") != 2 {
		t.Fatalf("list with a repeated --status = %q", got)
	}
	if got := c.planOK("list", "--search", "contention").stdout; got != "1\twon\tshard by key\n" {
		t.Fatalf("list --search = %q", got)
	}
	if got := c.planOK("list", "--search", "nothing like this").stdout; got != "" {
		t.Fatalf("list of no match = %q", got)
	}
	// An agent can take a plan up again: close names an end, and edit may reopen.
	if r := c.planOK("edit", "1", "--status", "open"); r.stdout != "1\topen\tshard by key\n" {
		t.Fatalf("reopen = %q", r.stdout)
	}
}

// Text may come in as an argument, as standard input, or as a file, and the plan holds the same
// text whichever way it came, for any text. The same holds for a comment and for a close.
func TestTextInputWaysAgree(t *testing.T) {
	c := newCLI(t)
	r := rand.New(rand.NewPCG(3, 4))
	pieces := []string{"a", " ", "\n", "\n\n", "ü", "# h", "- x", "  ", "`code`", "\t"}
	for i := range 40 {
		var b strings.Builder
		for range 1 + r.IntN(8) {
			b.WriteString(pieces[r.IntN(len(pieces))])
		}
		text := b.String()
		c.files["/in.md"] = text

		byArg := c.planJSON("create", "--title", "t", "--body", text)
		c.stdin = text
		byStdinDash := c.planJSON("create", "--title", "t", "--body", "-")
		byStdinFile := c.planJSON("create", "--title", "t", "--body-file", "-")
		byFile := c.planJSON("create", "--title", "t", "--body-file", "/in.md")
		for name, p := range map[string]planJSON{"--body -": byStdinDash, "--body-file -": byStdinFile, "--body-file": byFile} {
			if p.Body != byArg.Body || p.Body != text {
				t.Fatalf("round %d: %s stored %q, want %q", i, name, p.Body, text)
			}
		}

		if strings.TrimSpace(text) == "" {
			continue
		}
		c.stdin = text
		if got := c.planJSON("comment", fmt.Sprint(byArg.Number), "--body", "-"); got.Comments[len(got.Comments)-1].Text != text {
			t.Fatalf("round %d: comment stored %q, want %q", i, got.Comments[len(got.Comments)-1].Text, text)
		}
		if got := c.planJSON("close", fmt.Sprint(byFile.Number), "--status", "parked", "--comment-file", "/in.md"); got.Comments[0].Text != text || got.Status != "PLAN_STATUS_PARKED" {
			t.Fatalf("round %d: close = %+v", i, got)
		}
		c.stdin = ""
	}
}

func TestAnEditChangesOnlyWhatItNames(t *testing.T) {
	c := newCLI(t)
	c.planOK("create", "--title", "t", "--body", "keep me")
	if p := c.planJSON("edit", "1", "--title", "new"); p.Title != "new" || p.Body != "keep me" || p.Status != "PLAN_STATUS_OPEN" {
		t.Fatalf("title edit = %+v", p)
	}
	if p := c.planJSON("edit", "1", "--body", ""); p.Title != "new" || p.Body != "" {
		t.Fatalf("clearing the body = %+v, want the body empty and the title kept", p)
	}
	if p := c.planJSON("edit", "1", "--status", "parked"); p.Status != "PLAN_STATUS_PARKED" || p.Title != "new" {
		t.Fatalf("status edit = %+v", p)
	}
}

func TestPlanMistakesAreInvalidInputAndNeverStartTheDaemon(t *testing.T) {
	c := newCLI(t)
	c.files["/in.md"] = "x"
	cases := []struct {
		name string
		args []string
		want string
	}{
		{"no subcommand", []string{"plan"}, "subcommand"},
		{"unknown subcommand", []string{"plan", "delete"}, "delete"},
		{"no task", []string{"plan", "list"}, "--task"},
		{"create without title", []string{"plan", "create", "--task", "kv"}, "--title"},
		{"create with blank title", []string{"plan", "create", "--task", "kv", "--title", "  "}, "--title"},
		{"create with a stray argument", []string{"plan", "create", "--task", "kv", "--title", "t", "oops"}, "oops"},
		{"body given twice", []string{"plan", "create", "--task", "kv", "--title", "t", "--body", "a", "--body-file", "/in.md"}, "one way"},
		{"missing body file", []string{"plan", "create", "--task", "kv", "--title", "t", "--body-file", "/nope.md"}, "--body-file"},
		{"unknown flag", []string{"plan", "create", "--task", "kv", "--nope"}, "nope"},
		{"list unknown status", []string{"plan", "list", "--task", "kv", "--status", "done"}, "done"},
		{"list stray argument", []string{"plan", "list", "--task", "kv", "1"}, "unexpected"},
		{"view without number", []string{"plan", "view", "--task", "kv"}, "plan number"},
		{"view with two numbers", []string{"plan", "view", "--task", "kv", "1", "2"}, "plan number"},
		{"view a non-number", []string{"plan", "view", "--task", "kv", "first"}, "first"},
		{"view zero", []string{"plan", "view", "--task", "kv", "0"}, "plan number"},
		{"view negative", []string{"plan", "view", "--task", "kv", "-1"}, "-1"},
		{"edit nothing", []string{"plan", "edit", "--task", "kv", "1"}, "at least one"},
		{"edit unknown status", []string{"plan", "edit", "--task", "kv", "1", "--status", "done"}, "done"},
		{"comment nothing", []string{"plan", "comment", "--task", "kv", "1"}, "--body"},
		{"close without status", []string{"plan", "close", "--task", "kv", "1"}, "--status"},
		{"close unknown status", []string{"plan", "close", "--task", "kv", "1", "--status", "done"}, "done"},
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

// What the daemon refuses is reported as the daemon says it, with the exit code of its kind.
func TestPlanRefusalsFromTheDaemon(t *testing.T) {
	c := newCLI(t)
	c.planOK("create", "--title", "t")
	for _, tc := range []struct {
		name string
		args []string
		code int
		want string
	}{
		{"two-line title", []string{"create", "--title", "a\nb"}, exitInvalidInput, "one line"},
		{"long title", []string{"create", "--title", strings.Repeat("t", 300)}, exitInvalidInput, "title"},
		{"close as open", []string{"close", "1", "--status", "open"}, exitInvalidInput, "does not close"},
		{"empty comment", []string{"comment", "1", "--body", ""}, exitInvalidInput, "not empty"},
		{"unknown plan view", []string{"view", "9"}, exitFailed, "plan 9"},
		{"unknown plan comment", []string{"comment", "9", "--body", "c"}, exitFailed, "plan 9"},
		{"unknown plan close", []string{"close", "9", "--status", "won"}, exitFailed, "plan 9"},
		{"unknown plan edit", []string{"edit", "9", "--title", "t"}, exitFailed, "plan 9"},
	} {
		r := c.plan(tc.args[0], tc.args[1:]...)
		if r.code != tc.code || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%s: exit %d, stdout %q, stderr %q; want exit %d, %q in stderr", tc.name, r.code, r.stdout, r.stderr, tc.code, tc.want)
		}
	}
}

// Plans belong to a task: another task of the repository has its own plans and numbers.
func TestPlansBelongToTheTaskOfTheCommand(t *testing.T) {
	c := newCLI(t)
	c.planOK("create", "--title", "mine")
	r := c.run("plan", "create", "--task", "other", "--title", "theirs")
	if r.code != exitOK || r.stdout != "1\topen\ttheirs\n" {
		t.Fatalf("create in another task = %+v", r)
	}
	if got := c.planOK("list").stdout; got != "1\topen\tmine\n" {
		t.Fatalf("list = %q, want only this task's plan", got)
	}
}

// `submit --plan` links a job, and `plan view` lists it, with the job as `jobs` shows it.
func TestSubmitPlanLinksTheJobToThePlan(t *testing.T) {
	c := newCLI(t)
	c.planOK("create", "--title", "t")
	r := c.run(withTask("submit", "command", "HEAD", "--plan", "1", "--", "print", "x", ";", "exit", "0")...)
	if r.code != exitOK {
		t.Fatalf("submit --plan = %+v", r)
	}
	id := strings.Fields(r.stdout)[0]
	c.run(withTask("wait", id)...)
	other := c.submit("exit", "0")

	view := c.planOK("view", "1").stdout
	if !strings.Contains(view, "jobs (1):") || !strings.Contains(view, id+"\tsucceeded (exit 0)") || strings.Contains(view, other+"\t") {
		t.Fatalf("view = %q, want only job %s", view, id)
	}
	var out struct {
		View struct {
			Jobs []struct {
				Job struct {
					ID    string   `json:"id"`
					Plans []uint32 `json:"plans"`
				} `json:"job"`
			} `json:"jobs"`
		} `json:"view"`
	}
	if err := json.Unmarshal([]byte(c.planOK("view", "1", "--json").stdout), &out); err != nil {
		t.Fatal(err)
	}
	if jobs := out.View.Jobs; len(jobs) != 1 || jobs[0].Job.ID != id || len(jobs[0].Job.Plans) != 1 || jobs[0].Job.Plans[0] != 1 {
		t.Fatalf("view --json jobs = %+v", jobs)
	}

	r = c.run(withTask("submit", "command", "HEAD", "--plan", "7", "--", "exit", "0")...)
	if r.code != exitFailed || !strings.Contains(r.stderr, "plan 7") {
		t.Fatalf("submit --plan of an unknown plan = %+v", r)
	}
}

// --json is one message per line in the contract's field names, and an error is a Failure on stderr.
func TestPlanJSONOutput(t *testing.T) {
	c := newCLI(t)
	for _, args := range [][]string{
		{"create", "--title", "t", "--body", "b"},
		{"list"},
		{"view", "1"},
		{"edit", "1", "--title", "u"},
		{"comment", "1", "--body", "c"},
		{"close", "1", "--status", "rejected"},
	} {
		r := c.plan(args[0], append(args[1:], "--json")...)
		var v map[string]any
		if r.code != exitOK || strings.Count(r.stdout, "\n") != 1 || json.Unmarshal([]byte(r.stdout), &v) != nil {
			t.Errorf("plan %v --json = %+v", args, r)
		}
	}
	r := c.plan("view", "5", "--json")
	var f struct{ Kind, Message string }
	if r.code != exitFailed || r.stdout != "" || json.Unmarshal([]byte(r.stderr), &f) != nil || f.Kind != "FAILURE_KIND_NOT_FOUND" {
		t.Fatalf("view of an unknown plan --json = %+v", r)
	}
}
