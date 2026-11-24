package main

import (
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task/tasktest"
)

// A measure of a candidate built before a promotion moved the result branch is marked under its
// line, and named with the head in --json, while the measure of the new head is not; once the
// measures end, nothing is marked.
func TestJobsMarksAMeasureThatDoesNotContainTheResultHead(t *testing.T) {
	c := newResultsCLI(t)
	side := core.Commit(strings.Repeat("a", 40)) // built on base, beside c1
	c.world.Branches.AddCommit("/repo", side, rev(0))
	for _, commit := range []core.Commit{rev(0), rev(1), side} {
		c.world.Tasks.Put("/repo", commit, tasktest.Scripted("kv", []string{"hold", "g", ";", "exit", "0"}, []string{"emit", `{"ops": 7.5}`}))
	}
	measure := func(revision string) string {
		t.Helper()
		r := c.run(withTask("submit", "measure", revision, "--samples", "2", "--no-plan")...)
		mustExit(t, r, exitOK, "submit measure", revision)
		return strings.Fields(r.stdout)[0]
	}
	stale := measure(string(side))
	c.world.Branches.AddBranch("/repo", resultBranch, rev(1)) // a promotion moved the head
	current := measure("c1")

	r := c.run(withTask("jobs")...)
	mustExit(t, r, exitOK, "jobs")
	marker := "superseded: does not contain result head " + shortCommit(string(rev(1)))
	if strings.Count(r.stdout, marker) != 1 {
		t.Fatalf("jobs = %q, want one %q", r.stdout, marker)
	}
	lines := strings.Split(r.stdout, "\n")
	for i, line := range lines {
		if strings.Contains(line, marker) && (i == 0 || !strings.HasPrefix(lines[i-1], stale)) {
			t.Errorf("the marker is not right under %s's line: %q", stale, r.stdout)
		}
	}

	r = c.run(withTask("jobs", "--json")...)
	mustExit(t, r, exitOK, "jobs --json")
	var resp cliv1.ListJobsResponse
	if err := protojson.Unmarshal([]byte(r.stdout), &resp); err != nil {
		t.Fatalf("jobs --json = %q: %v", r.stdout, err)
	}
	got := resp.GetSuperseded()
	if len(got) != 1 || got[0].GetJobId() != stale || got[0].GetResultHead() != string(rev(1)) {
		t.Fatalf("superseded = %v, want only %s against %s", got, stale, rev(1))
	}

	c.world.Gates.Release("g")
	mustExit(t, c.run(withTask("wait", stale, current)...), exitOK, "wait")
	if r := c.run(withTask("jobs")...); r.code != exitOK || strings.Contains(r.stdout, "superseded") {
		t.Fatalf("jobs after the measures ended = %+v, want nothing marked", r)
	}
}

func TestASupersededMarkNamesTheShortHead(t *testing.T) {
	head := strings.Repeat("0123456789", 4)
	if got, want := describeSuperseded(head), "superseded: does not contain result head 012345678901, so promote refuses it"; got != want {
		t.Fatalf("describeSuperseded = %q, want %q", got, want)
	}
}
