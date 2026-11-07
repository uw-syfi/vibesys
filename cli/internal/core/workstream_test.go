package core_test

import (
	"errors"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func TestOnlyTheThreeNamedStatusesAreWorkstreamStatuses(t *testing.T) {
	want := []core.WorkstreamStatus{"active", "paused", "closed"}
	if !slices.Equal(core.WorkstreamStatuses(), want) {
		t.Fatalf("WorkstreamStatuses = %v, want %v", core.WorkstreamStatuses(), want)
	}
	for _, s := range want {
		if !s.Valid() {
			t.Errorf("%q is not valid", s)
		}
	}
	for _, s := range []core.WorkstreamStatus{"", "ACTIVE", "open", "done", "active "} {
		if s.Valid() {
			t.Errorf("%q was accepted as a status", s)
		}
	}
}

// pieces are what the titles below are made of: letters in both cases, the characters that are one
// letter in several cases (the Kelvin sign and k, the long s and s, the sharp s in both cases), white
// space of each kind, and characters no fold touches.
var titlePieces = []string{"a", "A", "k", "K", "K", "s", "S", "ſ", "ß", "ẞ", "ü", "Ü", "-", "7", " ", "  ", "\t", " ", " "}

func randomTitle(r interface{ IntN(int) int }) string {
	var b strings.Builder
	for range r.IntN(7) {
		b.WriteString(titlePieces[r.IntN(len(titlePieces))])
	}
	return b.String()
}

// Two titles have one key exactly when they are the same text once white space is collapsed and
// case is ignored, as strings.EqualFold ignores it: that is what makes two spellings of a name one
// name, and keeps two different names apart.
func TestTitleKeysAgreeWithCaseAndSpaceInsensitiveEquality(t *testing.T) {
	r := newRand(21)
	for range properties * 20 {
		a, b := randomTitle(r), randomTitle(r)
		same := strings.EqualFold(core.NormalizeWorkstreamTitle(a), core.NormalizeWorkstreamTitle(b))
		if got := core.WorkstreamTitleKey(a) == core.WorkstreamTitleKey(b); got != same {
			t.Fatalf("keys of %q and %q are equal: %v, but the titles are equal ignoring case and space: %v", a, b, got, same)
		}
	}
}

// A key is the key of its own normal form and of any other spelling of it.
func TestAKeyIsStableUnderNormalizingAndRespelling(t *testing.T) {
	r := newRand(22)
	for range properties * 20 {
		title := randomTitle(r)
		normal := core.NormalizeWorkstreamTitle(title)
		if core.NormalizeWorkstreamTitle(normal) != normal {
			t.Fatalf("normalizing %q twice gives %q, and once gives %q", title, core.NormalizeWorkstreamTitle(normal), normal)
		}
		if core.WorkstreamTitleKey(title) != core.WorkstreamTitleKey(normal) || core.WorkstreamTitleKey(normal) != core.WorkstreamTitleKey(core.WorkstreamTitleKey(normal)) {
			t.Fatalf("the key of %q is not the key of %q, or is not its own key", title, normal)
		}
		if normal != strings.TrimSpace(normal) || strings.Contains(normal, "  ") {
			t.Fatalf("normal form %q of %q has white space at an end or a run of it", normal, title)
		}
	}
}

// Whatever the text, deciding a title either fails with invalid input that names the field, or gives
// the title a workstream is stored under, which a workstream holds.
func TestDecidingATitleAcceptsOrNamesTheField(t *testing.T) {
	r := newRand(23)
	extra := []string{"\n", "\r", "\xff", strings.Repeat("é", 40), strings.Repeat("x", 90)}
	for range properties * 10 {
		title := randomTitle(r)
		if r.IntN(4) == 0 {
			title += extra[r.IntN(len(extra))]
		}
		got, err := core.DecideWorkstreamTitle("title", title)
		normal := core.NormalizeWorkstreamTitle(title)
		ok := strings.TrimSpace(title) != "" && !strings.ContainsAny(title, "\r\n") && utf8.ValidString(title) && utf8.RuneCountInString(normal) <= core.MaxWorkstreamTitleRunes
		if (err == nil) != ok {
			t.Fatalf("%q: err = %v, want accepted %v", title, err, ok)
		}
		if err != nil {
			var f *core.Failure
			if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.HasPrefix(f.Message, "title:") {
				t.Fatalf("%q: error = %v, want invalid input naming the title", title, err)
			}
			continue
		}
		if got != normal {
			t.Fatalf("%q decided as %q, want %q", title, got, normal)
		}
		w := core.Workstream{Number: 1, Title: got, Status: core.WorkstreamActive, Origin: core.RecordOrigin{At: epoch}, UpdatedAt: epoch}
		if err := w.Check(); err != nil {
			t.Fatalf("%q decided as %q, which a workstream does not hold: %v", title, got, err)
		}
	}
}

func TestATitleAtTheLimitIsHeldAndOnePastItIsNot(t *testing.T) {
	at := strings.Repeat("é", core.MaxWorkstreamTitleRunes)
	if got, err := core.DecideWorkstreamTitle("title", at); err != nil || got != at {
		t.Fatalf("a title of %d characters = %q, %v; want it kept", core.MaxWorkstreamTitleRunes, got, err)
	}
	if _, err := core.DecideWorkstreamTitle("title", at+"é"); err == nil {
		t.Fatal("a title one character past the limit was accepted")
	}
	// The limit is on the title as it is stored, so white space that collapses does not count.
	spaced := strings.Repeat("é  ", core.MaxWorkstreamTitleRunes/2)
	if got, err := core.DecideWorkstreamTitle("title", spaced); err != nil || utf8.RuneCountInString(got) > core.MaxWorkstreamTitleRunes {
		t.Fatalf("a title that is short once its white space collapses = %q, %v; want it accepted", got, err)
	}
}

func TestANoteIsOneLineWithinItsLimit(t *testing.T) {
	for note, ok := range map[string]bool{
		"":                      true,
		"paused: accuracy gate": true,
		"a\nb":                  false,
		"a\rb":                  false,
		"bad\xff":               false,
		strings.Repeat("é", core.MaxWorkstreamNoteRunes):      true,
		strings.Repeat("é", core.MaxWorkstreamNoteRunes+1):    false,
		strings.Repeat("é ", core.MaxWorkstreamNoteRunes/2):   true,
		strings.Repeat("é ", core.MaxWorkstreamNoteRunes/2+1): false,
	} {
		if err := core.CheckWorkstreamNote("note", note); (err == nil) != ok {
			t.Errorf("note of %d bytes: err = %v, want accepted %v", len(note), err, ok)
		}
	}
}

// A closed workstream is named by no title, so a line of work that was closed can start again, and
// two workstreams that are not closed never share a title, which CheckWorkstreamTitles finds.
func TestOnlyAWorkstreamThatIsNotClosedIsNamedByATitle(t *testing.T) {
	w := core.Workstream{Number: 1, Title: "KV cache", Status: core.WorkstreamActive}
	for _, s := range core.WorkstreamStatuses() {
		w.Status = s
		if got, want := w.Named("kv  CACHE"), s != core.WorkstreamClosed; got != want {
			t.Errorf("a %s workstream named by its title: %v, want %v", s, got, want)
		}
		if w.Named("KV cache layout") {
			t.Errorf("a %s workstream was named by another title", s)
		}
	}
	mk := func(n core.WorkstreamNumber, title string, s core.WorkstreamStatus) core.Workstream {
		return core.Workstream{Number: n, Title: title, Status: s}
	}
	ok := []core.Workstream{mk(1, "KV cache", core.WorkstreamClosed), mk(2, "kv cache", core.WorkstreamActive), mk(3, "KV Cache", core.WorkstreamClosed), mk(4, "Batching", core.WorkstreamPaused)}
	if err := core.CheckWorkstreamTitles(ok); err != nil {
		t.Fatalf("one open workstream per title: %v", err)
	}
	bad := append(slices.Clone(ok), mk(5, "KV  CACHE", core.WorkstreamPaused))
	var f *core.Failure
	if err := core.CheckWorkstreamTitles(bad); !errors.As(err, &f) || f.Kind != core.FailureConflict || !strings.Contains(f.Message, "2 and 5") {
		t.Fatalf("two open workstreams with one title: err = %v, want a conflict that names both", err)
	}
}
