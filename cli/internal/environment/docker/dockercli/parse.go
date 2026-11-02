package dockercli

import (
	"fmt"
	"regexp"
	"slices"
	"strings"
	"unicode"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

var (
	imageID  = regexp.MustCompile(`^sha256:[0-9a-f]{64}$`)
	platform = regexp.MustCompile(`^[a-z0-9]+/[a-z0-9_.-]+$`)

	namePattern     = regexp.MustCompile(`^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}$`)
	labelKeyPattern = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*$`)
	tagPattern      = regexp.MustCompile(`^[a-z0-9][a-z0-9._-]*(/[a-z0-9][a-z0-9._-]*)*(:[A-Za-z0-9_][A-Za-z0-9_.-]{0,127})?$`)
)

// parseImage reads the one line `docker image inspect --format '{{.Id}} {{.Os}}/{{.Architecture}}'`
// prints. It refuses anything else, so that an engine that answers in some other shape fails the
// call and does not pin a guess.
func parseImage(out string) (docker.Image, error) {
	fields := strings.Fields(out)
	if len(fields) != 2 || !imageID.MatchString(fields[0]) || !platform.MatchString(fields[1]) {
		return docker.Image{}, &core.Failure{Kind: core.FailureInternal, Message: fmt.Sprintf("dockercli: cannot read the image from %q", clip(out))}
	}
	return docker.Image{ID: fields[0], Platform: fields[1]}, nil
}

// parseNames reads the container names `docker ps --format '{{.Names}}'` prints, one line each.
// A container with several names has them joined with commas, and each is a name.
func parseNames(out string) []string {
	var names []string
	for _, line := range strings.FieldsFunc(out, func(r rune) bool { return r == '\n' || r == '\r' || r == ',' }) {
		if name := strings.TrimFunc(line, unicode.IsSpace); name != "" && !slices.Contains(names, name) {
			names = append(names, name)
		}
	}
	slices.Sort(names)
	return names
}

func sortedKeys(l docker.Labels) []string {
	keys := make([]string, 0, len(l))
	for k := range l {
		keys = append(keys, k)
	}
	slices.Sort(keys)
	return keys
}

func checkName(name string) error {
	if !namePattern.MatchString(name) {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not a container name", name)}
	}
	return nil
}

func checkLabel(k, v string) error {
	if !labelKeyPattern.MatchString(k) || strings.ContainsAny(v, "\x00\n") {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: the label %q=%q is not valid", k, v)}
	}
	return nil
}
