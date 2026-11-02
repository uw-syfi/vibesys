package docker

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"unicode"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// instruction is one instruction of a Dockerfile, with its continuation lines joined.
type instruction struct {
	// Line is the 1-based line the instruction starts on.
	Line int
	// Name is the instruction's keyword, in upper case.
	Name string
	// Args is everything after the keyword, with the comments and the line breaks of its
	// continuation lines removed.
	Args string
}

// heredocWord is a word that opens a here-document: <<EOF, <<-EOF, <<"EOF" or <<'EOF', which may
// start with a file descriptor number.
var heredocWord = regexp.MustCompile(`^[0-9]*<<(-?)(?:"([^"]+)"|'([^']+)'|([A-Za-z_][A-Za-z0-9_]*))$`)

// directive is a parser directive: a comment of the form "# name=value" at the top of the file.
var directive = regexp.MustCompile(`^#\s*([a-zA-Z][a-zA-Z0-9]*)\s*=\s*(.*?)\s*$`)

// parseDockerfile splits a Dockerfile into its instructions. It understands what decides where an
// instruction ends: comments, the escape directive, continuation lines and here-documents, whose
// bodies are not instructions. It never fails: text that is not a Dockerfile yields whatever
// instructions it holds, and CheckDockerfile judges them.
func parseDockerfile(text []byte) []instruction {
	text = bytes.TrimPrefix(text, []byte("\xef\xbb\xbf"))
	lines := strings.Split(string(text), "\n")
	for i, l := range lines {
		lines[i] = strings.TrimSuffix(l, "\r")
	}
	escape := byte('\\')
	i := 0
	for ; i < len(lines); i++ {
		m := directive.FindStringSubmatch(trimLeft(lines[i]))
		if m == nil {
			break
		}
		if strings.EqualFold(m[1], "escape") && (m[2] == "`" || m[2] == `\`) {
			escape = m[2][0]
		}
	}
	var out []instruction
	for i = 0; i < len(lines); i++ {
		line := trimLeft(lines[i])
		if line == "" || line[0] == '#' {
			continue
		}
		start := i + 1
		joined := line
		for continues(joined, escape) {
			joined = strings.TrimRightFunc(joined, unicode.IsSpace)
			joined = joined[:len(joined)-1]
			next := i + 1
			for next < len(lines) && isSkippable(lines[next]) {
				next++
			}
			if next >= len(lines) {
				i = len(lines)
				break
			}
			i = next
			joined += trimLeft(lines[i])
		}
		name, args := cutWord(joined)
		if name == "" {
			// Nothing but escape characters: a line that holds no instruction.
			continue
		}
		in := instruction{Line: start, Name: strings.ToUpper(name), Args: args}
		out = append(out, in)
		i = skipHeredocs(lines, i, in)
	}
	return out
}

// isSkippable is a line that sits inside a continuation without being part of it.
func isSkippable(line string) bool {
	t := trimLeft(line)
	return t == "" || t[0] == '#'
}

func trimLeft(s string) string { return strings.TrimLeftFunc(s, unicode.IsSpace) }

// cutWord splits off the first word of a line, which is the rest's keyword or flag.
func cutWord(s string) (word, rest string) {
	s = trimLeft(s)
	if i := strings.IndexFunc(s, unicode.IsSpace); i >= 0 {
		return s[:i], strings.TrimSpace(s[i:])
	}
	return s, ""
}

// continues reports whether a line ends with the escape character, so that the next line goes on.
func continues(line string, escape byte) bool {
	t := strings.TrimRightFunc(line, unicode.IsSpace)
	return t != "" && t[len(t)-1] == escape
}

// skipHeredocs returns the index of the last line of the here-documents the instruction opens,
// which are RUN, COPY and ADD's, or i when it opens none. A body that never ends takes the rest
// of the file, as it does for docker.
func skipHeredocs(lines []string, i int, in instruction) int {
	switch in.Name {
	case "RUN", "COPY", "ADD":
	default:
		return i
	}
	for _, w := range strings.Fields(in.Args) {
		m := heredocWord.FindStringSubmatch(w)
		if m == nil {
			continue
		}
		end := m[2] + m[3] + m[4]
		for i++; i < len(lines); i++ {
			body := lines[i]
			if m[1] == "-" {
				body = strings.TrimLeft(body, "\t")
			}
			if body == end {
				break
			}
		}
	}
	return min(i, len(lines)-1)
}

// CheckDockerfile refuses a Dockerfile that a build with no context cannot build, naming the line
// and what to do instead. The build sees the Dockerfile and no other file, so an instruction
// that copies a file of the task, or binds one in a RUN step, cannot work: it fails with
// core.FailureUnavailable. Text with no FROM instruction is core.FailureInvalidInput.
//
// It reads the Dockerfile as docker does for the cases that decide this, and judges nothing
// else: a Dockerfile it accepts can still fail to build, and the build's own error says why.
func CheckDockerfile(text []byte) error {
	ins := parseDockerfile(text)
	hasFrom := false
	for _, in := range ins {
		if in.Name == "FROM" {
			hasFrom = true
		}
		if what, needs := needsContext(in); needs {
			return &core.Failure{Kind: core.FailureUnavailable, Message: fmt.Sprintf(
				"docker: Dockerfile line %d: %s needs a build context, and the build of this tool has none (it sees only the Dockerfile); fetch the file in a RUN step, or COPY --from another image",
				in.Line, what)}
		}
	}
	if !hasFrom {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: "docker: the Dockerfile has no FROM instruction"}
	}
	return nil
}

// needsContext reports whether the instruction reads a file of the build context, and says which
// part of it does.
func needsContext(in instruction) (what string, needs bool) {
	switch in.Name {
	case "COPY", "ADD":
		flags, rest := splitFlags(in.Args)
		for _, f := range flags {
			if strings.HasPrefix(f, "--from=") && in.Name == "COPY" {
				return "", false
			}
		}
		sources := sourcesOf(rest)
		for _, s := range sources {
			if heredocWord.MatchString(s) || (in.Name == "ADD" && isRemote(s)) {
				continue
			}
			return fmt.Sprintf("%s of %q", in.Name, clip(s)), true
		}
	case "RUN":
		flags, _ := splitFlags(in.Args)
		for _, f := range flags {
			if m, ok := strings.CutPrefix(f, "--mount="); ok && mountsContext(m) {
				return "a RUN --mount of the context", true
			}
		}
	}
	return "", false
}

// splitFlags separates the leading --flags of an instruction from the rest.
func splitFlags(args string) (flags []string, rest string) {
	rest = strings.TrimSpace(args)
	for strings.HasPrefix(rest, "--") {
		var f string
		f, rest = cutWord(rest)
		flags = append(flags, f)
	}
	return flags, rest
}

// sourcesOf returns the sources of a COPY or ADD: all its arguments but the last, which is the
// destination. The JSON form lists them as an array.
func sourcesOf(rest string) []string {
	var words []string
	if strings.HasPrefix(rest, "[") {
		if err := json.Unmarshal([]byte(rest), &words); err != nil {
			words = nil
		}
	}
	if words == nil {
		words = strings.Fields(rest)
	}
	if len(words) < 2 {
		return nil
	}
	return words[:len(words)-1]
}

// isRemote reports whether an ADD source is fetched and not read from the context.
func isRemote(s string) bool {
	for _, p := range []string{"http://", "https://", "git@", "git://", "ssh://"} {
		if strings.HasPrefix(s, p) {
			return true
		}
	}
	return false
}

// mountsContext reports whether a RUN --mount value is a bind mount of the build context, which
// is what a bind mount without a "from" is.
func mountsContext(value string) bool {
	typ, hasFrom := "bind", false
	for _, kv := range strings.Split(value, ",") {
		k, v, _ := strings.Cut(kv, "=")
		switch strings.TrimSpace(k) {
		case "type":
			typ = strings.TrimSpace(v)
		case "from":
			hasFrom = true
		}
	}
	return typ == "bind" && !hasFrom
}

// clip shortens text for a message.
func clip(s string) string {
	const max = 60
	if len(s) <= max {
		return s
	}
	return strings.ToValidUTF8(s[:max], "") + "..."
}
