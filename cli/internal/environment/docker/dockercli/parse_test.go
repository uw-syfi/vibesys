package dockercli

// test-isolation: these parse the text the docker program prints and keep the tail of a build's
// log. They are pure, and through the Engine they are reachable only with a real daemon, where a
// fuzz target cannot feed them the text it wants. The real-engine tests cover them end to end.

import (
	"bytes"
	"errors"
	"fmt"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"unicode/utf8"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

func FuzzParseImage(f *testing.F) {
	id := "sha256:" + strings.Repeat("ab", 32)
	f.Add(id + " linux/amd64\n")
	f.Add("")
	f.Add(id)
	f.Add(id + " linux/amd64 extra")
	f.Add("sha256:xyz linux/amd64")
	f.Fuzz(func(t *testing.T, out string) {
		img, err := parseImage(out)
		if err != nil {
			return
		}
		// What it accepts is exactly one digest and one platform, however the text is spaced.
		if !imageID.MatchString(img.ID) || !platform.MatchString(img.Platform) {
			t.Fatalf("accepted %q as %+v", out, img)
		}
		if fields := strings.Fields(out); len(fields) != 2 || fields[0] != img.ID || fields[1] != img.Platform {
			t.Fatalf("accepted %q as %+v: not the two fields of the text", out, img)
		}
	})
}

func FuzzParseNames(f *testing.F) {
	f.Add("a\nb\n")
	f.Add("b,a\r\na\n")
	f.Add("")
	f.Add(" \n\n,")
	f.Fuzz(func(t *testing.T, out string) {
		names := parseNames(out)
		if !slices.IsSorted(names) {
			t.Fatalf("names %q are not sorted", names)
		}
		for i, name := range names {
			if name == "" || strings.ContainsAny(name, "\n\r,") || name != strings.TrimSpace(name) {
				t.Fatalf("name %q of %q is not a clean name", name, out)
			}
			if i > 0 && names[i-1] == name {
				t.Fatalf("name %q appears twice in %q", name, names)
			}
		}
		// The names, printed the way docker prints them, read back as the same names.
		if again := parseNames(strings.Join(names, "\n")); !slices.Equal(again, names) {
			t.Fatalf("names %q read back as %q", names, again)
		}
	})
}

// chunks cuts text into pieces at random places, as a pipe delivers it.
func chunks(rng *rand.Rand, text string) []string {
	var out []string
	for len(text) > 0 {
		n := 1 + rng.IntN(len(text))
		if rng.IntN(2) == 0 && n > 7 {
			n = 1 + rng.IntN(7)
		}
		out = append(out, text[:n])
		text = text[n:]
	}
	return out
}

// However a log is cut into writes, it passes on every byte unchanged, and the tail holds the last
// lines, which are the last lines of the whole text.
func TestTailKeepsTheLastLinesWhateverTheWriteBoundaries(t *testing.T) {
	for seed := range uint64(300) {
		rng := rand.New(rand.NewPCG(seed, 7))
		var text strings.Builder
		for range rng.IntN(80) {
			fmt.Fprintf(&text, "line %d %s", rng.IntN(1000), strings.Repeat("x", rng.IntN(30)))
			if rng.IntN(8) != 0 {
				text.WriteByte('\n')
			}
		}
		var passed bytes.Buffer
		tl := &tail{w: &passed}
		for _, c := range chunks(rng, text.String()) {
			if n, err := tl.Write([]byte(c)); n != len(c) || err != nil {
				t.Fatalf("seed %d: Write = %d, %v", seed, n, err)
			}
		}
		if passed.String() != text.String() {
			t.Fatalf("seed %d: the log was altered on the way", seed)
		}
		all := strings.Split(strings.TrimSuffix(text.String(), "\n"), "\n")
		if text.Len() == 0 {
			all = nil
		}
		if len(all) > tailLines {
			all = all[len(all)-tailLines:]
		}
		if got, want := tl.last(), strings.Join(all, "\n"); got != want {
			t.Fatalf("seed %d: last() =\n%q\nwant\n%q", seed, got, want)
		}
	}
}

// A build that prints for ever without ending a line does not grow the tail without bound, and
// what an error carries stays small and valid text.
func TestTailIsBoundedAgainstEndlessLines(t *testing.T) {
	tl := &tail{w: &bytes.Buffer{}}
	chunk := []byte(strings.Repeat("é", 1000))
	for range 400 {
		_, _ = tl.Write(chunk)
	}
	if len(tl.part) > maxLine {
		t.Fatalf("the unfinished line holds %d bytes, want at most %d", len(tl.part), maxLine)
	}
	last := tl.last()
	if len(last) > maxError || !utf8.ValidString(last) {
		t.Fatalf("last() is %d bytes, valid=%v", len(last), utf8.ValidString(last))
	}
}

func TestAnImageWithoutAShellIsRefusedByName(t *testing.T) {
	err := startFailure(`[FATAL tini (7)] exec /bin/sh failed: No such file or directory`)
	if err == nil || !strings.Contains(err.Error(), "no /bin/sh") || !strings.Contains(err.Error(), "--env host") {
		t.Fatalf("error %v does not say the image has no shell and what to do instead", err)
	}
}

func TestStartFailureTellsWhatWentWrongByKind(t *testing.T) {
	cases := []struct {
		stderr string
		kind   core.FailureKind
	}{
		{`docker: Error response from daemon: Conflict. The container name "/x" is already in use by container "abc".`, core.FailureConflict},
		{`docker: Error response from daemon: No such image: sha256:00`, core.FailureNotFound},
		{`docker: Cannot connect to the Docker daemon`, core.FailureUnavailable},
		{`[FATAL tini (7)] exec /bin/sh failed: No such file or directory`, core.FailureUnavailable},
		{``, core.FailureUnavailable},
	}
	for _, c := range cases {
		var f *core.Failure
		if err := startFailure(c.stderr); !errors.As(err, &f) || f.Kind != c.kind {
			t.Errorf("startFailure(%q) = %v, want a %s failure", c.stderr, err, c.kind)
		}
	}
}
