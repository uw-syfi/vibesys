package main

import (
	"encoding/json"
	"strings"
	"testing"
)

// The rollout of a new build, as an operator does it: drain the daemon, so queued jobs stay queued
// and say why; restart it, which keeps it draining; and resume, after which they run in their
// turn. A held job stays queued through all of it, saying who held it and why, until it is
// released. Nothing is cancelled and no job is submitted again.
func TestADrainedDaemonRestartsWithoutStartingAQueuedJob(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_AGENT"] = "lead"
	c.taskWithAJob("kv")

	if r := c.run("daemon", "drain", "--reason", "rolling out a new build"); r.code != exitOK || !strings.HasPrefix(r.stdout, "draining\tsince ") || !strings.Contains(r.stdout, "by lead\trolling out a new build") {
		t.Fatalf("daemon drain: %+v", r)
	}
	held, later := c.submit("exit", "0"), c.submit("exit", "0")
	if r := c.run(withTask("hold", held, "--reason", "after the integration check")...); r.code != exitOK || !strings.Contains(r.stdout, "held since ") || !strings.Contains(r.stdout, "by lead: after the integration check") {
		t.Fatalf("hold: %+v", r)
	}
	jobs := c.run(withTask("jobs", "--state", "queued")...)
	if jobs.code != exitOK || strings.Count(jobs.stdout, "waiting: the daemon is draining by lead: rolling out a new build") != 2 {
		t.Fatalf("jobs while draining, want both waiting on the drain:\n%s", jobs.stdout)
	}

	if r := c.run("daemon", "restart"); r.code != exitOK {
		t.Fatalf("daemon restart: %+v", r)
	}
	status := c.run("daemon", "status")
	if status.code != exitOK || !strings.Contains(status.stdout, "draining\tsince ") || !strings.Contains(status.stdout, "queued 2\trunning 0") {
		t.Fatalf("daemon status after the restart, want it draining with both jobs queued:\n%s", status.stdout)
	}
	var st struct {
		Drain struct {
			Agent  string `json:"agent"`
			Reason string `json:"reason"`
		} `json:"drain"`
	}
	if r := c.run("daemon", "status", "--json"); r.code != exitOK || json.Unmarshal([]byte(r.stdout), &st) != nil || st.Drain.Agent != "lead" || st.Drain.Reason != "rolling out a new build" {
		t.Fatalf("daemon status --json: %+v", r)
	}

	if r := c.run("daemon", "resume"); r.code != exitOK || r.stdout != "resumed\n" {
		t.Fatalf("daemon resume: %+v", r)
	}
	if r := c.run(withTask("wait", later)...); r.code != exitOK {
		t.Fatalf("wait for the job that is not held: %+v", r)
	}
	jobs = c.run(withTask("jobs", "--state", "queued")...)
	if !strings.Contains(jobs.stdout, held+"\tqueued") || !strings.Contains(jobs.stdout, "waiting: held by lead: after the integration check") {
		t.Fatalf("jobs after resuming, want the held job waiting on its hold:\n%s", jobs.stdout)
	}
	if r := c.run(withTask("release", held)...); r.code != exitOK || strings.Contains(r.stdout, "\t  held since") {
		t.Fatalf("release: %+v", r)
	}
	if r := c.run(withTask("wait", held)...); r.code != exitOK {
		t.Fatalf("wait for the released job: %+v", r)
	}
	all := c.run(withTask("jobs")...)
	if !strings.Contains(all.stdout, "note: held: after the integration check") || !strings.Contains(all.stdout, "note: released (held since ") {
		t.Fatalf("jobs, want the hold and the release in the job's notes:\n%s", all.stdout)
	}
	if status := c.run("daemon", "status"); strings.Contains(status.stdout, "draining") {
		t.Fatalf("daemon status after resuming still drains:\n%s", status.stdout)
	}
}

// Only a queued job is held: a hold of a job that ran is a conflict, exit 1, and changes nothing. A
// drain or a resume with no daemon running starts none and says so, exit 4.
func TestOnlyAQueuedJobIsHeldAndADaemonThatIsNotRunningIsNotDrained(t *testing.T) {
	c := newCLI(t)
	if r := c.run("daemon", "drain"); r.code != exitNoAnswer || r.stdout != "not running\n" || c.starter.Calls() != 0 {
		t.Fatalf("daemon drain with no daemon: %+v, %d daemons started", r, c.starter.Calls())
	}
	if r := c.run("daemon", "resume", "--json"); r.code != exitNoAnswer || !strings.Contains(r.stderr, "UNAVAILABLE") || c.starter.Calls() != 0 {
		t.Fatalf("daemon resume --json with no daemon: %+v", r)
	}
	c.taskWithAJob("kv")
	done := strings.Fields(c.run(withTask("jobs")...).stdout)[0]
	if r := c.run(withTask("hold", done)...); r.code != exitFailed || !strings.Contains(r.stderr, "only a queued job can be held") {
		t.Fatalf("hold of a finished job: %+v", r)
	}
	if r := c.run(withTask("hold", "j99")...); r.code != exitInvalidInput {
		t.Fatalf("hold of no such job: %+v", r)
	}
}

// A lead funds a plan at high priority and marks an exploratory job low, each with a reason: the
// plan line says its priority, `plan view` keeps the change as a comment, and the job's line says
// its own priority and keeps the change as a note. A change with no reason, or a reason with no
// change, is invalid input and starts no daemon.
func TestAPriorityChangeNeedsAReasonAndIsKept(t *testing.T) {
	c := newCLI(t)
	c.env["VIBESYS_AGENT"] = "lead"
	for _, args := range [][]string{
		jobPriority("j1", "high"),
		jobPriority("j1", "urgent", "--reason", "r"),
		jobPriority("j1"),
		{"job", "--task", "kv", "nice", "j1", "high", "--reason", "r"},
		{"plan", "edit", "1", "--task", "kv", "--priority", "high"},
		{"plan", "edit", "1", "--task", "kv", "--priority", "inherit", "--reason", "r"},
		{"plan", "edit", "1", "--task", "kv", "--title", "t", "--reason", "r"},
	} {
		if r := c.run(args...); r.code != exitInvalidInput {
			t.Errorf("%v: %+v, want invalid input", args, r)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatalf("an invalid priority change started a daemon")
	}
	c.taskWithAJob("kv")
	c.planOK("create", "--title", "the integration line")
	if r := c.planOK("edit", "1", "--priority", "high", "--reason", "gates every later plan"); r.stdout != "1\topen\tthe integration line\tpriority high\n" {
		t.Fatalf("plan edit --priority = %q", r.stdout)
	}
	view := c.planOK("view", "1").stdout
	if !strings.Contains(view, "lead, ") || !strings.Contains(view, "priority normal -> high: gates every later plan") {
		t.Fatalf("plan view, want the change as a comment by lead:\n%s", view)
	}
	if r := c.run("daemon", "drain"); r.code != exitOK {
		t.Fatalf("daemon drain: %+v", r)
	}
	job := c.submit("exit", "0")
	r := c.run(jobPriority(job, "low", "--reason", "exploratory")...)
	if r.code != exitOK || !strings.Contains(r.stdout, "\tpriority low") || !strings.Contains(r.stdout, "note: priority inherited -> low: exploratory") {
		t.Fatalf("job priority: %+v", r)
	}
	if r := c.run(jobPriority(job, "inherit", "--reason", "back to the plan", "--json")...); r.code != exitOK || strings.Contains(r.stdout, `"priority"`) {
		t.Fatalf("job priority inherit --json, want no priority of its own: %+v", r)
	}
}

// jobPriority is the arguments of `job priority` for the test task.
func jobPriority(args ...string) []string {
	return append([]string{"job", "priority", "--task", "kv"}, args...)
}
