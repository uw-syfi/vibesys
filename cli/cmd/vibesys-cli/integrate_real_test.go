package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// The integration of accepted plans that the combine-candidates skill describes, run for real:
// three plans are merged onto the head as a chain of prefix commits, the merged candidate's check
// fails because one plan breaks it, a bisection over the prefixes with checks only names that plan
// in at most two more checks, and the rest are checked and measured once with every job linked to
// every plan the candidate holds. Each step is a command the skill gives an agent.

// integrationTask is a task whose check passes while value.txt holds 42, and whose benchmark scores
// the lines of opts.txt, a file every plan appends to. Each check and each sample leaves a byte in a
// counter, so the test knows how many ran.
func (m *machine) integrationTask() {
	m.t.Helper()
	m.git("init", "-q", "-b", "main")
	m.write("value.txt", "42\n", 0o644)
	m.write("opts.txt", "base\n", 0o644)
	dir := task.TasksDir + "/bench/"
	m.write(dir+task.ManifestName, fmt.Sprintf(`
version = "0.1"
backends = ["cpu"]

[accuracy]
command = ['sh', '-c', 'printf c >> %s; test "$(cat ${PROJECT_ROOT}/value.txt)" = 42']
timeout_seconds = 60

[benchmark]
command = ['sh', '-c', 'printf x >> %s; printf "{\"ops\": %%s}" "$(wc -l < ${PROJECT_ROOT}/opts.txt)" > "$2"', 'sh']
timeout_seconds = 60

[benchmark.result]
json_argument = "--out"
metric = "ops"
`, m.checks(), m.counter), 0o644)
	m.write(dir+task.ObjectiveName, "[[objective]]\nname = \"ops\"\ndirection = \"max\"\n", 0o644)
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "task")
}

// checks is the file the task's check leaves a byte in each time it runs.
func (m *machine) checks() string { return filepath.Join(m.root, "checks") }

// checksRun is how many times the task's check has run.
func (m *machine) checksRun() int {
	m.t.Helper()
	data, err := os.ReadFile(m.checks())
	if err != nil && !os.IsNotExist(err) {
		m.t.Fatal(err)
	}
	return len(data)
}

// part is a plan accepted on cheap evidence: its commit and the commit it was built on.
type part struct {
	plan          string
	commit, built string
}

// acceptedPart builds a plan's candidate on base: it adds its line to opts.txt, the file every part
// shares, and, when it breaks, sets value.txt to what the check refuses.
func (m *machine) acceptedPart(plan, base, line string, breaks bool) part {
	m.t.Helper()
	m.git("checkout", "-q", "-b", "plan-"+plan, base)
	data, err := os.ReadFile(filepath.Join(m.repo, "opts.txt"))
	if err != nil {
		m.t.Fatal(err)
	}
	m.write("opts.txt", string(data)+line+"\n", 0o644)
	if breaks {
		m.write("value.txt", "41\n", 0o644)
	}
	m.git("commit", "-q", "-am", "feat: "+line)
	return part{plan: plan, commit: m.git("rev-parse", "HEAD"), built: base}
}

// apply applies a part's own change on the checkout and commits it, as the skill says: the diff
// from the commit it was built on, with three-way merging, and a conflict in the shared file
// resolved by keeping both sides, which for lines appended at one place is their union.
func (m *machine) apply(p part) string {
	m.t.Helper()
	diff := m.git("diff", p.built, p.commit)
	patch := filepath.Join(m.root, "part.patch")
	if err := os.WriteFile(patch, []byte(diff+"\n"), 0o644); err != nil {
		m.t.Fatal(err)
	}
	// A conflict makes git apply exit 1, so it does not go through m.git.
	cmd := exec.Command("git", "-C", m.repo, "apply", "--3way", patch)
	if out, err := cmd.CombinedOutput(); err != nil && !strings.Contains(string(out), "with conflicts") {
		m.t.Fatalf("git apply: %v\n%s", err, out)
	}
	opts := filepath.Join(m.repo, "opts.txt")
	data, err := os.ReadFile(opts)
	if err != nil {
		m.t.Fatal(err)
	}
	var kept []string
	for _, l := range strings.Split(string(data), "\n") {
		if !strings.HasPrefix(l, "<<<<<<<") && !strings.HasPrefix(l, "=======") && !strings.HasPrefix(l, ">>>>>>>") {
			kept = append(kept, l)
		}
	}
	m.write("opts.txt", strings.Join(kept, "\n"), 0o644)
	m.git("add", "-A")
	m.git("commit", "-q", "-m", "feat(integrate): apply plan "+p.plan)
	return m.git("rev-parse", "HEAD")
}

// check submits the task's check of a commit linked to a plan, waits, and says whether it passed
// and which job answered.
func (m *machine) check(commit, plan string) (bool, string) {
	m.t.Helper()
	r := m.cli("submit", "check", commit, "--plan", plan, "--wait")
	if r.code != exitOK && r.code != exitFailed {
		m.t.Fatalf("submit check %s = %+v", commit, r)
	}
	return r.code == exitOK, strings.Fields(r.stdout)[0]
}

// createPlan creates a plan and returns its number.
func (m *machine) createPlan(args ...string) string {
	m.t.Helper()
	r := m.cli(append([]string{"plan", "create"}, args...)...)
	if r.code != exitOK {
		m.t.Fatalf("plan create %v = %+v", args, r)
	}
	return strings.Fields(r.stdout)[0]
}

func TestRealIntegrationBisectionNamesThePlanThatBreaksTheCheck(t *testing.T) {
	t.Parallel()
	for culprit := range 3 {
		t.Run(fmt.Sprintf("plan %d of 3 breaks the check", culprit+1), func(t *testing.T) {
			t.Parallel()
			m := newMachine(t)
			m.integrationTask()
			head := m.git("rev-parse", "HEAD")
			if r := m.cli("submit", "check", head, "--wait"); r.code != exitOK {
				t.Fatalf("the head's check = %+v", r)
			}

			var parts []part
			for i, line := range []string{"a", "b", "c"} {
				plan := m.createPlan("--title", "plan "+line+" adds its option", "--kind", "kernel")
				parts = append(parts, m.acceptedPart(plan, head, line, i == culprit))
			}
			integration := m.createPlan("--title", "plans 1, 2, 3 together", "--kind", "integration", "--depends-on", "1,2,3")

			// The chain: C0 is the head, and Ck holds the first k parts.
			m.git("checkout", "-q", "-b", "integrate", head)
			chain := []string{head}
			for _, p := range parts {
				chain = append(chain, m.apply(p))
			}
			// Every part appended to the one shared file, so the second and third conflicted;
			// the resolution kept each part's line.
			if got := m.git("show", chain[3]+":opts.txt"); got != "base\na\nb\nc" {
				t.Fatalf("opts.txt of the merged candidate = %q, want every part's line", got)
			}
			if ok, _ := m.check(chain[3], integration); ok {
				t.Fatal("the merged candidate passed a check one of its plans breaks")
			}

			// The bisection, checks only: lo passes, hi fails.
			before := m.checksRun()
			lo, hi := 0, len(parts)
			for hi-lo > 1 {
				mid := (lo + hi) / 2
				if ok, _ := m.check(chain[mid], integration); ok {
					lo = mid
				} else {
					hi = mid
				}
			}
			extra := m.checksRun() - before
			t.Logf("bisection named plan %s in %d extra checks", parts[hi-1].plan, extra)
			if hi-1 != culprit || extra > 2 {
				t.Fatalf("the bisection named part %d in %d checks, want part %d in at most 2", hi, extra, culprit+1)
			}
			// Alone or with another: the part's own candidate, built on the head, fails alone.
			if hi > 1 {
				if ok, _ := m.check(parts[hi-1].commit, parts[hi-1].plan); ok {
					t.Fatal("the part named passed alone, yet only it breaks the check")
				}
			}

			// Finish without it, from the longest prefix that passed: one check, one measure.
			held := []part{}
			for i, p := range parts {
				if i != culprit {
					held = append(held, p)
				}
			}
			top := chain[lo]
			m.git("checkout", "-q", "-B", "integrate", top)
			for _, p := range parts[hi:] {
				top = m.apply(p)
			}
			if top != chain[lo] {
				if ok, _ := m.check(top, integration); !ok {
					t.Fatal("the integration without the part at fault failed its check")
				}
			}
			m.integratedOnce(top, integration, held)
			// The skill's budget: 1 + F*(ceil(log2 N) + 2) checks, with one part at fault among
			// three, beside the head's own check.
			if used := m.checksRun() - 1; used > 1+1*(2+2) {
				t.Errorf("the integration ran %d checks, over the budget of 5 the skill states", used)
			}
		})
	}
}

// integratedOnce checks and measures the candidate once, linked to the integration plan and, by
// repeating each submission, to every part it holds; the repeats run nothing.
func (m *machine) integratedOnce(top, integration string, held []part) {
	m.t.Helper()
	_, checkJob := m.check(top, integration)
	checks := m.checksRun()
	r := m.cli("submit", "measure", top, "--samples", "2", "--plan", integration)
	if r.code != exitOK {
		m.t.Fatalf("submit measure = %+v", r)
	}
	measureJob := strings.Fields(r.stdout)[0]
	for _, p := range held {
		if _, job := m.check(top, p.plan); job != checkJob {
			m.t.Errorf("linking the check to plan %s answered with %s, want the check %s", p.plan, job, checkJob)
		}
		r := m.cli("submit", "measure", top, "--samples", "2", "--plan", p.plan)
		if r.code != exitOK || strings.Fields(r.stdout)[0] != measureJob {
			m.t.Errorf("linking the measure to plan %s = %+v, want the measure %s", p.plan, r, measureJob)
		}
	}
	if r := m.cli("wait", measureJob); r.code != exitOK {
		m.t.Fatalf("wait %s = %+v", measureJob, r)
	}
	if m.checksRun() != checks || m.samples() != 2 {
		m.t.Errorf("%d checks and %d samples ran for the links, want none beyond the one measure's 2 samples", m.checksRun()-checks, m.samples())
	}
	deps := make([]string, 0, len(held))
	for _, p := range held {
		deps = append(deps, p.plan)
		if r := m.cli("plan", "view", p.plan); !strings.Contains(r.stdout, measureJob) || !strings.Contains(r.stdout, checkJob) {
			m.t.Errorf("plan view %s = %+v, want the check %s and the measure %s listed", p.plan, r, checkJob, measureJob)
		}
	}
	if r := m.cli("plan", "edit", integration, "--depends-on", strings.Join(deps, ",")); r.code != exitOK {
		m.t.Fatalf("plan edit --depends-on = %+v", r)
	}
	if r := m.cli("plan", "view", integration); !strings.Contains(r.stdout, measureJob) || !strings.Contains(r.stdout, "depends on:") {
		m.t.Errorf("plan view %s = %+v, want the measure and the plans it holds", integration, r)
	}
}
