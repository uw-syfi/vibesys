package main

import (
	"bytes"
	"fmt"
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"testing/quick"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/propcheck"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Every value of each enum, the ones the contract names and any other a newer peer may send, maps
// to one of the five codes, and only a success is 0.
func TestEveryEndingHasACodeAndOnlySuccessIsZero(t *testing.T) {
	valid := func(c int) bool { return c >= exitOK && c <= exitNoAnswer }
	failure := func(n int32) bool {
		c := exitOfFailure(cliv1.FailureKind(n))
		_, named := cliv1.FailureKind_name[n]
		return valid(c) && c != exitOK && (named || c == exitNoAnswer)
	}
	successes := map[cliv1.WaitOutcomeKind]bool{
		cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_ALL_SUCCEEDED:   true,
		cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_FIRST_SUCCEEDED: true,
	}
	wait := func(n int32) bool {
		k := cliv1.WaitOutcomeKind(n)
		c := exitOfWait(k)
		_, named := cliv1.WaitOutcomeKind_name[n]
		return valid(c) && (c == exitOK) == successes[k] && (named || c == exitNoAnswer)
	}
	for _, p := range []func(int32) bool{failure, wait} {
		if err := quick.Check(p, propcheck.Config(t, &quick.Config{MaxCount: 2000})); err != nil {
			t.Fatal(err)
		}
		for n := range int32(16) {
			if !p(n) {
				t.Fatalf("value %d", n)
			}
		}
	}
	for a, want := range map[answer]int{answerYes: exitOK, answerNo: exitFailed, answerUnread: exitNoAnswer, answer(9): exitNoAnswer} {
		if got := exitOfAnswer(a); got != want {
			t.Errorf("answer %d exits %d, want %d", a, got, want)
		}
	}
	if answerOf(false, false) != answerUnread {
		t.Error("a response with neither arm is read as an answer")
	}
}

// The README's exit table names, for each code, the contract values that end in it. Every value
// the contract has is in the row of the code the code gives it, and in no other.
func TestTheReadmeExitTableIsTheCode(t *testing.T) {
	raw, err := os.ReadFile("../../README.md")
	if err != nil {
		t.Fatal(err)
	}
	row := regexp.MustCompile("(?m)^\\| ([0-4]) \\| [^|]+ \\| ([^|]+) \\|$")
	rows := row.FindAllStringSubmatch(string(raw), -1)
	if len(rows) != 5 {
		t.Fatalf("the README has %d exit rows, want 5", len(rows))
	}
	listed := map[string]int{}
	for _, r := range rows {
		code, _ := strconv.Atoi(r[1])
		for _, name := range regexp.MustCompile("`([A-Z_]+)`").FindAllStringSubmatch(r[2], -1) {
			if prev, dup := listed[name[1]]; dup {
				t.Errorf("%s is in rows %d and %d", name[1], prev, code)
			}
			listed[name[1]] = code
		}
	}
	check := func(name string, got int) {
		if want, ok := listed[name]; !ok || want != got {
			t.Errorf("%s exits %d, and the README says %d (listed %v)", name, got, want, ok)
		}
	}
	for n, name := range cliv1.FailureKind_name {
		check(strings.TrimPrefix(name, "FAILURE_KIND_"), exitOfFailure(cliv1.FailureKind(n)))
	}
	for n, name := range cliv1.WaitOutcomeKind_name {
		short := strings.TrimPrefix(name, "WAIT_OUTCOME_KIND_")
		if short == "UNSPECIFIED" {
			continue // one UNSPECIFIED row entry stands for both enums, and both are 4
		}
		check(short, exitOfWait(cliv1.WaitOutcomeKind(n)))
	}
	if exitOfWait(cliv1.WaitOutcomeKind_WAIT_OUTCOME_KIND_UNSPECIFIED) != listed["UNSPECIFIED"] {
		t.Error("a wait outcome of UNSPECIFIED does not exit as the README says")
	}
	if len(listed) != len(cliv1.FailureKind_name)+len(cliv1.WaitOutcomeKind_name)-1 {
		t.Errorf("the README lists %v, which is not exactly the contract's values", listed)
	}
}

// A failure kind from a newer daemon is printed as the daemon sent it, its number included, and is
// no answer: never 0, and never rewritten to INTERNAL. In a wait, any failure that is not about the
// input is no answer too.
func TestAFailureKindThisBuildDoesNotKnowIsKeptAndIsNoAnswer(t *testing.T) {
	newer := &wire.Received{Failure: &cliv1.Failure{Kind: cliv1.FailureKind(99), Message: "from a newer daemon"}}
	conflict := &wire.Received{Failure: &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_CONFLICT, Message: "held"}}
	cases := []struct {
		name string
		run  func(d deps) int
		kind string
		code int
	}{
		{"a command", func(d deps) int { return fail(d, true, newer) }, `"kind":99`, exitNoAnswer},
		{"a wait", func(d deps) int { return failWait(d, true, fmt.Errorf("%w: %w", client.ErrEndUnknown, newer)) }, `"kind":99`, exitNoAnswer},
		{"a conflict in a wait", func(d deps) int { return failWait(d, true, fmt.Errorf("%w: %w", client.ErrEndUnknown, conflict)) }, `"kind":"FAILURE_KIND_CONFLICT"`, exitNoAnswer},
		{"a wait no daemon answered", func(d deps) int { return failWait(d, true, fmt.Errorf("%w: gone", client.ErrEndUnknown)) }, `"kind":"FAILURE_KIND_UNAVAILABLE"`, exitNoAnswer},
	}
	for _, tc := range cases {
		var stdout, stderr bytes.Buffer
		code := tc.run(deps{stdout: &stdout, stderr: &stderr})
		if code != tc.code || stdout.Len() != 0 || !strings.Contains(stderr.String(), tc.kind) {
			t.Errorf("%s: exit %d, stderr %q; want exit %d and %s", tc.name, code, stderr.String(), tc.code, tc.kind)
		}
	}
}
