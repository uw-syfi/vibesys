package shorttmp_test

import (
	"os"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/shorttmp"
)

func TestMain(m *testing.M) { os.Exit(shorttmp.Run(m)) }

// Whatever the caller's TMPDIR and whichever candidates work, the choice is the caller's own when
// it is short and usable, and otherwise a short usable candidate when there is one.
func TestChooseIsShortWheneverAShortUsableDirectoryExists(t *testing.T) {
	err := quick.Check(func(current string, currentOK bool, names []string, okMask uint16) bool {
		usable := map[string]bool{}
		cands := make([]string, len(names))
		for i, n := range names {
			cands[i] = "/" + n
			usable[cands[i]] = okMask&(1<<(i%16)) != 0
		}
		usable[current] = currentOK
		got := shorttmp.Choose(current, cands, func(d string) bool { return usable[d] })
		switch {
		case current != "" && len(current) <= shorttmp.MaxLen && currentOK:
			return got == current
		case got == current:
			for _, c := range cands {
				if len(c) <= shorttmp.MaxLen && usable[c] {
					return false
				}
			}
			return true
		default:
			return len(got) <= shorttmp.MaxLen && usable[got]
		}
	}, nil)
	if err != nil {
		t.Fatal(err)
	}
}

func TestAnUnusableCandidateIsSkippedForTheNext(t *testing.T) {
	long := "/" + strings.Repeat("d", 80)
	got := shorttmp.Choose(long, []string{"/a", "/b"}, func(d string) bool { return d == "/b" })
	if got != "/b" {
		t.Fatalf("Choose = %q, want /b", got)
	}
}

func TestUsableAcceptsADirectoryASocketBindsIn(t *testing.T) {
	if !shorttmp.Usable(t.TempDir()) {
		t.Fatal("a fresh temporary directory is not usable for sockets")
	}
	if shorttmp.Usable(t.TempDir() + "/missing") {
		t.Fatal("a missing directory is usable")
	}
}
