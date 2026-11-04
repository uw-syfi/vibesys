package daemon

import (
	"slices"
	"strings"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
)

// Inherited is what every job inherits from the daemon, which the status reports so that a user
// can tell whether a job will see a variable without running one.
type Inherited struct {
	// Environ is the daemon's environment, as os.Environ returns it. The supervisors and their jobs
	// are started with it.
	Environ []string
	// CheckoutDirectory is the directory clean checkouts are made in.
	CheckoutDirectory string
}

// shownNames are the variables whose value the status shows: they say where a job runs and what
// it finds, and they hold no secret.
var shownNames = []string{"HOME", "PATH", "TMPDIR"}

// shownPrefix is the prefix of the variables a task reads, whose value the status also shows when
// the name does not look like a secret.
const shownPrefix = "VIBESYS_"

// secretWords mark a name whose value is never shown, whatever its prefix.
var secretWords = []string{"KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL"}

// showsValue says whether the status shows the value of the variable name.
func showsValue(name string) bool {
	if slices.Contains(shownNames, name) {
		return true
	}
	if !strings.HasPrefix(name, shownPrefix) {
		return false
	}
	upper := strings.ToUpper(name)
	return !slices.ContainsFunc(secretWords, func(w string) bool { return strings.Contains(upper, w) })
}

// inheritedVariables returns each variable of environ once, by name, with its value only when
// showsValue allows it. A later entry of a name wins, as it does for a process started with it.
func inheritedVariables(environ []string) []*cliv1.InheritedVariable {
	values := map[string]string{}
	for _, kv := range environ {
		name, value, ok := strings.Cut(kv, "=")
		if !ok || name == "" {
			continue
		}
		values[name] = value
	}
	names := make([]string, 0, len(values))
	for name := range values {
		names = append(names, name)
	}
	slices.Sort(names)
	out := make([]*cliv1.InheritedVariable, 0, len(names))
	for _, name := range names {
		v := &cliv1.InheritedVariable{Name: name}
		if showsValue(name) {
			value := values[name]
			v.Value = &value
		}
		out = append(out, v)
	}
	return out
}
