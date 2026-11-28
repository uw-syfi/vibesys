package main

import (
	"regexp"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/clock"
)

// contextFlags is a context for a call, flag by flag, each with its value.
var contextFlags = [][]string{{"--task", "kv"}, {"--run=run-a"}, {"-harness", "h"}, {"--agent", "a"}, {"--json"}}

// Regression: an agent that wrote `vibesys-cli --task t jobs` got the usage and exit 2. The context
// flags may come before the command, and a command line answers the same wherever they are.
func TestContextFlagsMayComeBeforeTheCommand(t *testing.T) {
	c := newCLI(t)
	job := c.submit("exit", "0")
	if r := c.run(withTask("wait", job)...); r.code != exitOK {
		t.Fatalf("wait = %+v", r)
	}
	if r := c.run("--task", "kv", "jobs"); r.code != exitOK || !strings.Contains(r.stdout, job) {
		t.Fatalf("--task kv jobs = %+v, want the job listed", r)
	}
	lines := [][]string{{"jobs"}, {"wait", job}, {"show", "HEAD"}, {"wait", "j404"}, {"wait", job, "--bogus"}}
	property := func(which uint8, mask uint8) bool {
		line := lines[int(which)%len(lines)]
		var before, after []string
		for i, f := range contextFlags {
			if mask&(1<<i) != 0 {
				before = append(before, f...)
			} else {
				after = append(after, f...)
			}
		}
		want := c.run(slices.Concat(line, before, after)...)
		got := c.run(slices.Concat(before, line, after)...)
		if got != want {
			t.Logf("%v before %v: %+v; after it: %+v", before, line, got, want)
		}
		return got == want
	}
	if err := quick.Check(property, nil); err != nil {
		t.Fatal(err)
	}
}

// What follows a lone "--" is the job's, so a context flag before the command never lands in it.
func TestContextFlagsBeforeTheCommandStayOutOfTheJobsArgv(t *testing.T) {
	c := newCLI(t)
	r := c.run("--task", "kv", "submit", "command", "--image", "harness", "HEAD", "--env", "host", "--wait", "--", "print", "--run", ";", "exit", "0")
	if r.code != exitOK {
		t.Fatalf("submit = %+v", r)
	}
	logs := c.run(withTask("logs", strings.Fields(r.stdout)[0])...)
	if !strings.Contains(logs.stdout, "--run") || strings.Contains(logs.stdout, "kv") {
		t.Fatalf("logs = %+v, want the job's own argument and not the context", logs)
	}
}

// Anything before the command but a context flag is refused, naming the word and saying where flags
// go, and so is a context flag given on both sides of the command. The daemon is never reached.
func TestOnlyTheContextComesBeforeTheCommand(t *testing.T) {
	c := newCLI(t)
	cases := []struct {
		args []string
		want string
	}{
		{[]string{"--any", "wait", "j1"}, "put flags after the subcommand: vibesys-cli <cmd> --task ..."},
		{[]string{"--timeout", "2h", "wait", "j1", "--task", "kv"}, "--timeout comes before the command"},
		{[]string{"--task", "kv", "jobs", "--task", "other"}, "--task is given before the command and after it"},
		{[]string{"-task=kv", "jobs", "--task=other"}, "--task is given before the command and after it"},
		{[]string{"--task", "kv"}, "name a command"},
		{[]string{"--task", "kv", "--", "jobs"}, "name a command"},
		{[]string{"--task"}, "--task: give it a value"},
		{[]string{"--task", "kv", "bogus"}, "usage: vibesys-cli"},
	}
	for _, tc := range cases {
		r := c.run(tc.args...)
		if r.code != exitInvalidInput || r.stdout != "" || !strings.Contains(r.stderr, tc.want) {
			t.Errorf("%v = %+v, want exit 2 and %q", tc.args, r, tc.want)
		}
	}
	if c.starter.Calls() != 0 {
		t.Fatal("an invalid invocation started a daemon")
	}
}

// oneDashFlag finds a flag spelled with one dash, as the flag package spells it.
var oneDashFlag = regexp.MustCompile(`(^|\s)-[A-Za-z]`)

// Regression: the flag package's errors and help spelled `flag -timeout` while the CLI's usage, its
// own errors and its README spell `--timeout`, and agents took one spelling for the only one. Every
// flag error and every command's help spells a flag with two dashes.
func TestFlagErrorsAndHelpSpellFlagsWithTwoDashes(t *testing.T) {
	c := newCLI(t)
	for _, args := range [][]string{
		{"wait", "j1", "--timeout", "20"},
		{"wait", "j1", "-timeout=soon"},
		{"wait", "j1", "--nope"},
		{"wait", "j1", "--any=maybe"},
		{"wait", "j1", "--timeout"},
		{"jobs", "-state", "stuck"},
		{"submit", "command", "HEAD", "--image", "harness", "--gpus", "x", "--", "true"},
		{"wait", "-h"},
		{"submit", "command", "-h"},
		{"plan", "create", "-h"},
		{"alloc", "wait", "-h"},
	} {
		r := c.run(args...)
		if oneDashFlag.MatchString(r.stderr) || !strings.Contains(r.stderr, " --") {
			t.Errorf("%v: stderr %q spells a flag with one dash", args, r.stderr)
		}
		r = c.run(append(args, "--json")...)
		if oneDashFlag.MatchString(r.stderr) {
			t.Errorf("%v --json: stderr %q spells a flag with one dash", args, r.stderr)
		}
	}
	if r := c.run("wait", "j1", "--timeout", "20"); !strings.Contains(r.stderr, `invalid value "20" for flag --timeout`) {
		t.Errorf("a timeout with no unit: %q, want the flag named --timeout", r.stderr)
	}
}

// A flag parses with one dash or two, with its value apart or after "=": every spelling of wait's
// timeout is the same timeout.
func TestEverySpellingOfAFlagParses(t *testing.T) {
	c := newCLI(t)
	c.starter.Timers = clock.Exactly{D: waitTimeout, Others: clock.System{}}
	id := c.submit("hold", "g", ";", "exit", "0")
	d := waitTimeout.String()
	for _, spelling := range [][]string{{"-timeout", d}, {"--timeout", d}, {"-timeout=" + d}, {"--timeout=" + d}} {
		if r := c.run(withTask(append([]string{"wait", id}, spelling...)...)...); r.code != exitTimeout {
			t.Errorf("wait %v = %+v, want the timeout, exit 3", spelling, r)
		}
	}
	c.world.Gates.Release("g")
}
