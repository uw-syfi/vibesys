package core_test

import (
	"errors"
	"slices"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestOnlyTheFiveNamedStatusesAreValidAndOnlyOpenDoesNotClose(t *testing.T) {
	want := []core.PlanStatus{"open", "chosen", "won", "parked", "rejected"}
	if !slices.Equal(core.PlanStatuses(), want) {
		t.Fatalf("PlanStatuses = %v, want %v", core.PlanStatuses(), want)
	}
	for _, s := range want {
		if !s.Valid() || s.Closes() != (s != core.PlanOpen) {
			t.Errorf("%q: valid %v, closes %v", s, s.Valid(), s.Closes())
		}
	}
	r := newRand(7)
	for range properties {
		s := core.PlanStatus(strings.Repeat("x", r.IntN(6)) + string(rune('a'+r.IntN(26))))
		if slices.Contains(want, s) {
			continue
		}
		if s.Valid() || s.Closes() {
			t.Fatalf("%q was accepted as a status", s)
		}
	}
	if core.PlanStatus("").Valid() || core.PlanStatus("OPEN").Valid() {
		t.Fatal("an empty or upper-case status was accepted")
	}
}

// Whatever the text, a check either accepts it or fails with invalid input that names the field.
func TestPlanTextChecksAcceptOrNameTheField(t *testing.T) {
	r := newRand(11)
	pieces := []string{"a", " ", "\n", "\r", "ü", "\xff", "# h", "x"}
	for range properties {
		var b strings.Builder
		for range r.IntN(8) {
			b.WriteString(pieces[r.IntN(len(pieces))])
		}
		text := b.String()
		for _, c := range []struct {
			name  string
			check func(string, string) error
			ok    bool
		}{
			{"title", core.CheckPlanTitle, strings.TrimSpace(text) != "" && !strings.ContainsAny(text, "\r\n") && !strings.Contains(text, "\xff")},
			{"body", core.CheckPlanBody, !strings.Contains(text, "\xff")},
			{"comment", core.CheckPlanComment, strings.TrimSpace(text) != "" && !strings.Contains(text, "\xff")},
		} {
			err := c.check(c.name, text)
			if (err == nil) != c.ok {
				t.Fatalf("%s %q: err = %v, want accepted %v", c.name, text, err, c.ok)
			}
			var f *core.Failure
			if err != nil && (!errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.HasPrefix(f.Message, c.name+":")) {
				t.Fatalf("%s %q: err = %v, want invalid input naming the field", c.name, text, err)
			}
		}
	}
}

func TestPlanTextLimitsAreExact(t *testing.T) {
	if core.CheckPlanTitle("title", strings.Repeat("t", core.MaxPlanTitleBytes)) != nil || core.CheckPlanTitle("title", strings.Repeat("t", core.MaxPlanTitleBytes+1)) == nil {
		t.Error("the title limit is not exact")
	}
	if core.CheckPlanBody("body", strings.Repeat("b", core.MaxPlanBodyBytes)) != nil || core.CheckPlanBody("body", strings.Repeat("b", core.MaxPlanBodyBytes+1)) == nil {
		t.Error("the body limit is not exact")
	}
	if core.CheckPlanComment("c", strings.Repeat("c", core.MaxPlanCommentBytes)) != nil || core.CheckPlanComment("c", strings.Repeat("c", core.MaxPlanCommentBytes+1)) == nil {
		t.Error("the comment limit is not exact")
	}
}

func TestLinkedToKeepsJobsThatLinkToThePlanInOrder(t *testing.T) {
	r := newRand(5)
	for range properties {
		var jobs []core.Job
		var want []core.JobID
		for i := range r.IntN(10) {
			j := core.Job{ID: core.JobID(strings.Repeat("j", i+1))}
			for range r.IntN(3) {
				j.Plans = append(j.Plans, core.PlanNumber(1+r.IntN(4)))
			}
			if slices.Contains(j.Plans, 2) {
				want = append(want, j.ID)
			}
			jobs = append(jobs, j)
		}
		var got []core.JobID
		for _, j := range core.LinkedTo(2, jobs) {
			got = append(got, j.ID)
		}
		if !slices.Equal(got, want) {
			t.Fatalf("LinkedTo = %v, want %v", got, want)
		}
	}
}
