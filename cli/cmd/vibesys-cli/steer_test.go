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
