package ledger_test

import (
	"errors"
	"math"
	"reflect"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/ledger"
)

func wantInvalid(t *testing.T, what string, err error, path string) {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		t.Errorf("%s: error = %v, want invalid_input", what, err)
		return
	}
	if !strings.Contains(f.Message, path) {
		t.Errorf("%s: the message %q does not name %s", what, f.Message, path)
	}
}

func TestHoldRefusesWhatNoStoreCanKeep(t *testing.T) {
	nan, inf := math.NaN(), math.Inf(-1)
	badTime := time.Date(10000, 1, 1, 0, 0, 0, 0, time.UTC)
	early := time.Date(-1, 1, 1, 0, 0, 0, 0, time.UTC)
	plan := core.Plan{Title: "ok"}
	cases := []struct {
		name string
		call func() error
		path string
	}{
		{"invalid UTF-8 in a field", func() error { _, err := ledger.Hold("note", core.Note{Text: "a\xffb"}); return err }, "note.Text"},
		{"invalid UTF-8 in a map key", func() error {
			_, err := ledger.Hold("fp", core.Fingerprint{Facts: map[string]string{"k\xff": "v"}})
			return err
		}, "fp.Facts"},
		{"invalid UTF-8 in a map value", func() error {
			_, err := ledger.Hold("fp", core.Fingerprint{Facts: map[string]string{"k": "v\xc0"}})
			return err
		}, "fp.Facts"},
		{"invalid UTF-8 deep in a slice", func() error {
			_, err := ledger.Hold("job", core.Job{Notes: []core.Note{{Text: "fine"}, {Agent: "\xed\xa0\x80"}}})
			return err
		}, "job.Notes[1].Agent"},
		{"NaN", func() error { _, err := ledger.Hold("v", core.Verdict{CandidateMean: nan}); return err }, "v.CandidateMean"},
		{"infinity in a slice", func() error { _, err := ledger.Hold("o", core.MeasureOutcome{Samples: []float64{1, inf}}); return err }, "o.Samples[1]"},
		{"a time after year 9999", func() error { _, err := ledger.Hold("n", core.Note{At: badTime}); return err }, "n.At"},
		{"a time before year 0", func() error { _, err := ledger.Hold("n", core.Note{At: early}); return err }, "n.At"},
		{"a time behind a pointer", func() error {
			_, err := ledger.Hold("job", core.Job{StartedAt: &badTime})
			return err
		}, "job.StartedAt"},
		{"a pointer in an interface", func() error {
			_, err := ledger.Hold("e", core.EventDraft{Detail: &plan})
			return err
		}, "e.Detail"},
		{"a bad value inside an interface", func() error {
			_, err := ledger.Hold("e", core.EventDraft{Detail: core.Verdict{BaseMean: nan}})
			return err
		}, "e.Detail.BaseMean"},
	}
	for _, c := range cases {
		wantInvalid(t, c.name, c.call(), c.path)
	}
}

func TestHoldCanonicalForm(t *testing.T) {
	t.Run("empty slices and maps are nil", func(t *testing.T) {
		got, err := ledger.Hold("job", core.Job{Notes: []core.Note{}, Plans: []core.PlanNumber{}, Environment: &core.Fingerprint{Facts: map[string]string{}}})
		if err != nil {
			t.Fatal(err)
		}
		if got.Notes != nil || got.Plans != nil || got.Environment.Facts != nil {
			t.Errorf("held form = %+v, want nil slices and maps", got)
		}
	})

	t.Run("a time is the same instant in UTC without a monotonic reading", func(t *testing.T) {
		zone := time.FixedZone("x", -7*3600)
		in := time.Now().In(zone)
		got, err := ledger.Hold("n", core.Note{At: in})
		if err != nil {
			t.Fatal(err)
		}
		if !got.At.Equal(in) || got.At.Location() != time.UTC {
			t.Errorf("held time = %v, want %v in UTC", got.At, in)
		}
		if !reflect.DeepEqual(got.At, got.At.Round(0)) {
			t.Errorf("the held time keeps a monotonic reading")
		}
	})

	t.Run("a value holds to the same value twice", func(t *testing.T) {
		property := func(text string, x float64, secs int64, n uint8) bool {
			note := core.Note{Text: text, At: time.Unix(secs%1e11, int64(n)), Agent: text}
			outcome := core.MeasureOutcome{Samples: []float64{x}, Observations: []core.Observation{{Name: text, Values: nil}}}
			first, err := ledger.Hold("v", struct {
				Note    core.Note
				Outcome core.MeasureOutcome
			}{note, outcome})
			if err != nil {
				return true // refused is a different property
			}
			second, err := ledger.Hold("v", first)
			return err == nil && reflect.DeepEqual(first, second)
		}
		if err := quick.Check(property, nil); err != nil {
			t.Error(err)
		}
	})
}

func TestHoldCopiesDeeply(t *testing.T) {
	started := time.Date(2026, 11, 1, 0, 0, 0, 0, time.UTC)
	code := 3
	in := core.Job{
		ID: "j1", Spec: core.JobSpec{Settings: core.CommandSettings{Argv: []string{"a", "b"}, Service: &core.ServiceSpec{Argv: []string{"s"}}}},
		Notes: []core.Note{{Text: "n"}}, Plans: []core.PlanNumber{1}, StartedAt: &started, ExitCode: &code,
		Environment: &core.Fingerprint{Facts: map[string]string{"k": "v"}},
	}
	held, err := ledger.Hold("job", in)
	if err != nil {
		t.Fatal(err)
	}
	want, _ := ledger.Hold("job", in)

	// Change everything the caller still holds.
	in.Spec.Settings.(core.CommandSettings).Argv[0] = "changed"
	in.Spec.Settings.(core.CommandSettings).Service.Argv[0] = "changed"
	in.Notes[0].Text = "changed"
	in.Plans[0] = 99
	started = started.Add(time.Hour)
	code = 99
	in.Environment.Facts["k"] = "changed"
	if !reflect.DeepEqual(held, want) {
		t.Errorf("a held value changed with the value it came from:\n%+v\nwant\n%+v", held, want)
	}

	// And the other way: changing the held value leaves a copy of it alone.
	copied := ledger.Copy(held)
	held.Notes[0].Text = "changed"
	*held.StartedAt = held.StartedAt.Add(time.Hour)
	held.Environment.Facts["k"] = "changed"
	if copied.Notes[0].Text != "n" || !copied.StartedAt.Equal(started.Add(-time.Hour)) || copied.Environment.Facts["k"] != "v" {
		t.Errorf("a copy shares memory with its source: %+v", copied)
	}
}

func TestCopyOfAValueThatCannotBeHeldPanics(t *testing.T) {
	defer func() {
		if recover() == nil {
			t.Error("Copy of a NaN did not panic")
		}
	}()
	ledger.Copy(core.Verdict{BaseMean: math.NaN()})
}

func TestCheckName(t *testing.T) {
	for _, ok := range []string{"a", "A9", "run-1", "a.b_c-d", "0", "_x", "-x", strings.Repeat("a", 128)} {
		if err := ledger.CheckName("name", ok); err != nil {
			t.Errorf("CheckName(%q) = %v, want nil", ok, err)
		}
	}
	for _, bad := range []string{"", ".", "..", ".hidden", "a/b", "a b", "a\x00", "é", "a\n", strings.Repeat("a", 129), "../x", "a:b"} {
		wantInvalid(t, "CheckName("+bad+")", ledger.CheckName("name", bad), "name")
	}
	// Whatever is accepted is safe as one path element on any filesystem the store runs on.
	property := func(s string) bool {
		if ledger.CheckName("n", s) != nil {
			return true
		}
		return s != "" && s != "." && s != ".." && !strings.ContainsAny(s, "/\\\x00:") && !strings.HasPrefix(s, ".") && len(s) <= 128
	}
	if err := quick.Check(property, nil); err != nil {
		t.Error(err)
	}
}

func TestJobIDsRoundTrip(t *testing.T) {
	property := func(n uint64) bool {
		if n == 0 {
			_, ok := ledger.JobNumber(ledger.JobIDFor(0))
			return !ok
		}
		got, ok := ledger.JobNumber(ledger.JobIDFor(n))
		return ok && got == n
	}
	if err := quick.Check(property, nil); err != nil {
		t.Error(err)
	}
	for _, bad := range []core.JobID{"", "j", "j0", "j01", "j-1", "J1", "1", "j1 ", "j+1", "jx", "j18446744073709551616"} {
		if n, ok := ledger.JobNumber(bad); ok {
			t.Errorf("JobNumber(%q) = %d, true; want false", bad, n)
		}
	}
}
