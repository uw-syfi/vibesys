package doctor

import (
	"fmt"
	"strings"
)

// Quote returns s as one POSIX shell word that expands to exactly s.
func Quote(s string) string {
	return "'" + strings.ReplaceAll(s, "'", `'\''`) + "'"
}

// Export is one environment change a shell should make.
type Export struct {
	Name  string
	Value string
	// Prepend puts Value first in a path list (Name's current value follows) instead of
	// replacing Name's value.
	Prepend bool
}

// Line returns the export as a line a POSIX shell can evaluate.
func (e Export) Line() string {
	if e.Prepend {
		return fmt.Sprintf("export %s=%s\"${%s:+:$%s}\"", e.Name, Quote(e.Value), e.Name, e.Name)
	}
	return fmt.Sprintf("export %s=%s", e.Name, Quote(e.Value))
}

// MergeExports drops an export a later one makes redundant: a repeated prepend of the same
// directory to the same variable keeps its first place; a later replacement of a variable
// wins. The order of the rest is kept.
func MergeExports(all []Export) []Export {
	var out []Export
	for _, e := range all {
		dup := false
		for i, kept := range out {
			if kept.Name != e.Name {
				continue
			}
			switch {
			case kept.Prepend && e.Prepend && kept.Value == e.Value:
				dup = true
			case !kept.Prepend && !e.Prepend:
				out[i] = e
				dup = true
			}
		}
		if !dup {
			out = append(out, e)
		}
	}
	return out
}
