package devlaunch

import (
	"encoding/json"
	"fmt"
	"strconv"
	"strings"
)

// probeScript runs on the machine with the candidate path as $1 (`~` and `~/` mean the home
// directory) and prints what is there: `missing`, or `plain` or `git` with the absolute path, and
// for git the HEAD commit, the branch and the count of tracked files that differ from HEAD.
const probeScript = `p=$1; case $p in "~") p=$HOME ;; "~/"*) p="$HOME/${p#??}" ;; esac; ` +
	`cd "$p" 2>/dev/null || { echo missing; exit 0; }; here=$(pwd -P); ` +
	`if [ "$(git rev-parse --show-toplevel 2>/dev/null)" = "$here" ]; then ` +
	`echo git; echo "$here"; git rev-parse HEAD; git rev-parse --abbrev-ref HEAD; ` +
	`git status --porcelain --untracked-files=no | wc -l; else echo plain; echo "$here"; fi`

// ProbeCommand is the argument vector (run through the machine's login shell by ssh) that probes
// one candidate checkout path.
func ProbeCommand(candidate string) []string {
	return []string{"sh", "-c", shellQuote(probeScript), "vibesys-dev-probe", shellQuote(candidate)}
}

// ParseProbe reads probeScript's output. found is false when the path does not exist.
func ParseProbe(out string) (state RemoteState, found bool, err error) {
	lines := strings.Split(strings.TrimSpace(out), "\n")
	for i := range lines {
		lines[i] = strings.TrimSpace(lines[i])
	}
	switch lines[0] {
	case "missing":
		return RemoteState{}, false, nil
	case "plain":
		if len(lines) != 2 || lines[1] == "" {
			return RemoteState{}, false, fmt.Errorf("unreadable probe of the checkout: %q", out)
		}
		return RemoteState{Kind: CheckoutPlain, Checkout: lines[1]}, true, nil
	case "git":
		if len(lines) != 5 || lines[1] == "" || lines[2] == "" || lines[3] == "" {
			return RemoteState{}, false, fmt.Errorf("unreadable probe of the checkout: %q", out)
		}
		changed, convErr := strconv.Atoi(lines[4])
		if convErr != nil || changed < 0 {
			return RemoteState{}, false, fmt.Errorf("unreadable change count %q in the probe of the checkout", lines[4])
		}
		return RemoteState{Kind: CheckoutGit, Checkout: lines[1], Head: lines[2], Branch: lines[3], Dirty: changed > 0}, true, nil
	}
	return RemoteState{}, false, fmt.Errorf("unreadable probe of the checkout: %q", out)
}

// shellQuote wraps s in single quotes for a remote shell. Callers pass no quote, newline or NUL
// (see ValidateRemotePath), so the quoting needs no escape.
func shellQuote(s string) string { return "'" + s + "'" }

// ValidateRemotePath rejects a path that cannot cross an ssh command line intact.
func ValidateRemotePath(path string) error {
	switch {
	case path == "":
		return fmt.Errorf("the checkout path is empty")
	case strings.ContainsAny(path, "'\n\r\x00\\"):
		return fmt.Errorf("the checkout path %q contains a quote, backslash or line break", path)
	case strings.HasPrefix(path, "-"):
		return fmt.Errorf("the checkout path %q starts with a dash", path)
	case !(path == "~" || strings.HasPrefix(path, "~/") || strings.HasPrefix(path, "/")):
		return fmt.Errorf("the checkout path %q must be absolute or start with ~/", path)
	}
	return nil
}

// Hello is the part of a helper's hello reply this command reads.
type Hello struct {
	Version  string
	Root     string
	Commit   string
	HasCheck bool
}

// ParseHello reads one reply line, naming the reply type when it is not a hello.
func ParseHello(line []byte) (Hello, error) {
	var raw struct {
		Type     string `json:"type"`
		Version  string `json:"vibesys_version"`
		Checkout *struct {
			Root   string  `json:"root"`
			Commit *string `json:"commit"`
		} `json:"checkout"`
		Message string `json:"message"`
	}
	if err := json.Unmarshal(line, &raw); err != nil {
		return Hello{}, fmt.Errorf("reply is not JSON: %w", err)
	}
	if raw.Type != "hello" {
		return Hello{}, fmt.Errorf("reply of type %q, not hello: %s", raw.Type, truncate(string(line), 200))
	}
	h := Hello{Version: raw.Version}
	if raw.Checkout != nil {
		h.HasCheck, h.Root = true, raw.Checkout.Root
		if raw.Checkout.Commit != nil {
			h.Commit = *raw.Checkout.Commit
		}
	}
	return h, nil
}

// HelloReady reports whether a hello shows the helper runs the shipped code: it runs from a
// checkout and, when wantCommit is not empty, from that commit. seen says what the hello showed.
func HelloReady(h Hello, wantCommit string) (ready bool, seen string) {
	seen = fmt.Sprintf("version %s, root %s, commit %s", h.Version, orNone(h.Root), orNone(h.Commit))
	if !h.HasCheck {
		return false, seen
	}
	return wantCommit == "" || h.Commit == wantCommit, seen
}

func orNone(s string) string {
	if s == "" {
		return "none"
	}
	return s
}

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n] + "..."
}
