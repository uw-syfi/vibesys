package main

import (
	"os"
	"regexp"
	"strconv"
	"strings"
	"testing"
	"testing/quick"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
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
		if err := quick.Check(p, &quick.Config{MaxCount: 2000}); err != nil {
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
