package core_test

import (
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// FuzzCommitValid feeds the full-commit-id check any bytes. A commit it accepts is exactly 40
// lowercase hex digits, so nothing else can reach a git command line as a commit.
func FuzzCommitValid(f *testing.F) {
	for _, seed := range []string{"", "HEAD", "main", strings.Repeat("a", 40), strings.Repeat("A", 40), strings.Repeat("a", 39), strings.Repeat("a", 41), "-" + strings.Repeat("a", 39), strings.Repeat("g", 40)} {
		f.Add(seed)
	}
	f.Fuzz(func(t *testing.T, s string) {
		if !core.Commit(s).Valid() {
			return
		}
		if len(s) != 40 || strings.Trim(s, "0123456789abcdef") != "" {
			t.Fatalf("Commit(%q) is valid, but is not 40 lowercase hex digits", s)
		}
	})
}
