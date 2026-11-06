package wire

import "fmt"

// NamePattern is what an operation, capture or reference name must match (loadgen.proto's
// RequestsHeader.operation_names, Capture.name).
const NamePattern = `^[a-z][a-z0-9_]{0,62}$`

// ValidName reports whether name matches NamePattern, by hand: a regular expression per step
// showed in profiles.
func ValidName(name string) bool {
	if len(name) < 1 || len(name) > 63 || name[0] < 'a' || name[0] > 'z' {
		return false
	}
	for i := 1; i < len(name); i++ {
		c := name[i]
		if !(c >= 'a' && c <= 'z' || c >= '0' && c <= '9' || c == '_') {
			return false
		}
	}
	return true
}

// CheckOperationNames says what is wrong with a request stream's operation names, in words a
// workload's author can act on: loadgen refuses the stream at its start otherwise.
func CheckOperationNames(names []string) error {
	if len(names) == 0 {
		return fmt.Errorf("the workload has no operations")
	}
	seen := make(map[string]bool, len(names))
	for _, name := range names {
		if !ValidName(name) {
			return fmt.Errorf("operation name %q is not one loadgen takes: a lowercase letter, then up to 62 lowercase letters, digits and underscores (%s; no dots or dashes)", name, NamePattern)
		}
		if seen[name] {
			return fmt.Errorf("two operations are named %q: operation names are unique", name)
		}
		seen[name] = true
	}
	return nil
}
