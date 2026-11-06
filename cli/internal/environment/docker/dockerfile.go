package docker

import (
	"regexp"
	"slices"
	"strings"
)

// baseRefs returns the images outside the build that a Dockerfile builds on, in the order they
// first appear and once each: the image of each FROM, and of each COPY --from, that is not an
// earlier stage of the same Dockerfile, a stage's index, or scratch. An ARG declared before the
// first FROM is substituted with its default, which is its value since a build of this package
// passes no build argument. A reference that still names a variable after that is left out: no
// image can be looked up by it.
//
// The engine builds on its local image of such a reference when it has one, and pulls it only when
// it has none, so the image of the same files under the same reference changes when that local
// image does. That is why an image is named by them as well as by its files (buildKey).
func baseRefs(dockerfile []byte) []string {
	var (
		refs    []string
		globals = map[string]string{} // ARGs before the first FROM, by name
		stages  = map[string]bool{}   // stage names, lower case
		inStage bool
	)
	add := func(ref string) {
		ref = expand(ref, globals)
		lower := strings.ToLower(ref)
		if ref == "" || strings.Contains(ref, "$") || lower == "scratch" || stages[lower] || slices.Contains(refs, ref) {
			return
		}
		refs = append(refs, ref)
	}
	for _, line := range instructions(dockerfile) {
		fields := strings.Fields(line)
		switch strings.ToUpper(fields[0]) {
		case "ARG":
			if inStage {
				continue
			}
			for _, arg := range fields[1:] {
				if name, value, ok := strings.Cut(arg, "="); ok {
					globals[name] = strings.Trim(value, `"'`)
				}
			}
		case "FROM":
			inStage = true
			args := withoutFlags(fields[1:])
			if len(args) == 0 {
				continue
			}
			add(args[0])
			if len(args) >= 3 && strings.EqualFold(args[1], "AS") {
				stages[strings.ToLower(args[2])] = true
			}
		case "COPY":
			for _, f := range fields[1:] {
				if from, ok := strings.CutPrefix(f, "--from="); ok && !stageIndex.MatchString(from) {
					add(from)
				}
			}
		}
	}
	return refs
}

var (
	stageIndex = regexp.MustCompile(`^[0-9]+$`)
	directive  = regexp.MustCompile(`^#\s*([a-zA-Z]+)\s*=\s*(\S+)\s*$`)
	variable   = regexp.MustCompile(`\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)`)
)

// instructions returns a Dockerfile's instructions, each on one line: comments and blank lines are
// dropped, and a line that ends with the escape character (a backslash, or what an escape parser
// directive names) is joined to the next.
func instructions(dockerfile []byte) []string {
	lines := strings.Split(strings.ReplaceAll(string(dockerfile), "\r\n", "\n"), "\n")
	escape := `\`
	for _, line := range lines {
		m := directive.FindStringSubmatch(line)
		if m == nil {
			break
		}
		if strings.EqualFold(m[1], "escape") {
			escape = m[2]
		}
	}
	var (
		out     []string
		pending string
	)
	for _, line := range lines {
		trimmed := strings.TrimSpace(line)
		if trimmed == "" || strings.HasPrefix(trimmed, "#") {
			continue
		}
		if joined, ok := strings.CutSuffix(trimmed, escape); ok {
			pending += joined + " "
			continue
		}
		out = append(out, pending+trimmed)
		pending = ""
	}
	if strings.TrimSpace(pending) != "" {
		out = append(out, pending)
	}
	return out
}

// withoutFlags returns an instruction's arguments without its --flags.
func withoutFlags(args []string) []string {
	var out []string
	for _, a := range args {
		if !strings.HasPrefix(a, "--") {
			out = append(out, a)
		}
	}
	return out
}

// expand substitutes each $name and ${name} that globals has a value for.
func expand(s string, globals map[string]string) string {
	return variable.ReplaceAllStringFunc(s, func(v string) string {
		m := variable.FindStringSubmatch(v)
		name := m[1] + m[2]
		if value, ok := globals[name]; ok {
			return value
		}
		return v
	})
}
