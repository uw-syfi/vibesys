package core_test

import (
	"path"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// A path the caller gives a lab is either refused, or read as a clean relative path that, joined
// to the artifacts directory, stays inside it: no input climbs out.
func TestALabPathNeverLeavesTheArtifactsDirectory(t *testing.T) {
	const root = "/vibesys/artifacts"
	parts := []string{"", ".", "..", "a", "b", "/", root, root + "/", "c/../d", "../..", "x.y"}
	check := func(picks []uint8) bool {
		var b strings.Builder
		for _, p := range picks {
			b.WriteString(parts[int(p)%len(parts)])
			if p%3 == 0 {
				b.WriteString("/")
			}
		}
		in := b.String()
		got, err := core.ParseLabPath(in, root)
		if err != nil {
			return true
		}
		joined := got.Join(root)
		clean := got == "" || path.Clean(string(got)) == string(got)
		inside := joined == root || strings.HasPrefix(joined, root+"/")
		return clean && inside && path.Clean(joined) == joined && !strings.HasPrefix(string(got), "/")
	}
	if err := quick.Check(check, &quick.Config{MaxCount: 5000}); err != nil {
		t.Fatal(err)
	}
	for in, want := range map[string]core.LabPath{"": "", "a/b/": "a/b", root + "/x/y": "x/y", root: ""} {
		if got, err := core.ParseLabPath(in, root); err != nil || got != want {
			t.Errorf("ParseLabPath(%q) = %q, %v; want %q", in, got, err, want)
		}
	}
	for _, in := range []string{"..", "a/../../b", "/etc/passwd", root + "x/y", "a\x00b"} {
		if got, err := core.ParseLabPath(in, root); err == nil {
			t.Errorf("ParseLabPath(%q) = %q, want refused", in, got)
		}
	}
}

// A placement caps a lab's time at what it runs a command for; one with no cap leaves it.
func TestAPlacementCapsALabsTime(t *testing.T) {
	check := func(asked, longest uint32) bool {
		o := core.PlacementOffer{LongestRun: durationOf(longest)}
		got := o.LabTime(durationOf(asked))
		if o.LongestRun == 0 {
			return got == durationOf(asked)
		}
		return got == min(durationOf(asked), o.LongestRun)
	}
	if err := quick.Check(check, nil); err != nil {
		t.Fatal(err)
	}
}

func durationOf(minutes uint32) time.Duration { return time.Duration(minutes%100000) * time.Minute }
