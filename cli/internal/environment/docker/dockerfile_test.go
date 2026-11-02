package docker_test

import (
	"bytes"
	"errors"
	"math/rand/v2"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

func failure(t *testing.T, err error) *core.Failure {
	t.Helper()
	var f *core.Failure
	if !errors.As(err, &f) {
		t.Fatalf("error %v is not a *core.Failure", err)
	}
	return f
}

// The Dockerfile of a real task: build arguments with defaults, a pinned base, no context.
const realisticDockerfile = `# syntax=docker/dockerfile:1
ARG PLATFORM=linux/amd64
FROM --platform=${PLATFORM} ubuntu:24.04@sha256:33ceb71981b602c1a7443a53469e4dba065f7503eab3078a2d7a57a2ab987517 AS base

ARG TOOL_VERSION=1.2.3
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
    && apt-get install --yes --no-install-recommends \
        build-essential curl \
    && rm -rf /var/lib/apt/lists/*
# a comment inside the file
COPY --from=ghcr.io/astral-sh/uv:0.9.7@sha256:ba4857bf2a068e9bc0e64eed8563b065908a4cd6bfb66b531a9c424c8e25e142 /uv /uvx /usr/local/bin/
ADD https://example.com/tool.tar.gz /opt/
RUN --mount=type=cache,target=/root/.cache true
WORKDIR /workspace
`

func TestARealisticDockerfileIsAccepted(t *testing.T) {
	if err := docker.CheckDockerfile([]byte(realisticDockerfile)); err != nil {
		t.Fatal(err)
	}
}

func TestWhatNeedsABuildContextIsRefusedAndNamed(t *testing.T) {
	cases := map[string]struct {
		text, want string
	}{
		"a local COPY":                   {"FROM x\nCOPY run.py /opt/run.py\n", `COPY of "run.py"`},
		"a local ADD":                    {"FROM x\nADD data /data\n", `ADD of "data"`},
		"a JSON COPY":                    {"FROM x\nCOPY [\"a b\", \"/dst/\"]\n", `COPY of "a b"`},
		"a COPY with a flag":             {"FROM x\nCOPY --chown=1000:1000 f /f\n", `COPY of "f"`},
		"a continued COPY":               {"FROM x\nCOPY \\\n  f \\\n  /f\n", `COPY of "f"`},
		"a bind mount of the context":    {"FROM x\nRUN --mount=type=bind,source=a,target=/a ls /a\n", "RUN --mount"},
		"a default mount of the context": {"FROM x\nRUN --mount=source=a,target=/a ls /a\n", "RUN --mount"},
		"a mix with a remote ADD":        {"FROM x\nADD https://e.com/a /a\nADD local /b\n", `ADD of "local"`},
	}
	for name, c := range cases {
		t.Run(name, func(t *testing.T) {
			f := failure(t, docker.CheckDockerfile([]byte(c.text)))
			if f.Kind != core.FailureUnavailable || !strings.Contains(f.Message, c.want) || !strings.Contains(f.Message, "build context") {
				t.Fatalf("got %v, want unavailable naming %s and the build context", f, c.want)
			}
		})
	}
}

func TestWhatNeedsNoBuildContextIsAccepted(t *testing.T) {
	for name, text := range map[string]string{
		"COPY from a stage":        "FROM x AS a\nFROM y\nCOPY --from=a /f /f\n",
		"COPY from an image":       "FROM x\nCOPY --from=ghcr.io/o/i:1 /f /f\n",
		"a heredoc COPY":           "FROM x\nCOPY <<EOF /etc/conf\nkey=value\nEOF\n",
		"a quoted heredoc COPY":    "FROM x\nCOPY <<'EOF' /etc/conf\nCOPY secret /nowhere\nEOF\n",
		"a remote ADD":             "FROM x\nADD https://e.com/a.tgz /a\n",
		"a git ADD":                "FROM x\nADD git@github.com:o/r.git /r\n",
		"a cache mount":            "FROM x\nRUN --mount=type=cache,target=/c true\n",
		"a mount from an image":    "FROM x\nRUN --mount=type=bind,from=y,source=/a,target=/a true\n",
		"a COPY in a comment":      "FROM x\n# COPY secret /nowhere\n",
		"a COPY in a RUN heredoc":  "FROM x\nRUN <<EOF\nCOPY secret /nowhere\nEOF\n",
		"a dashed heredoc":         "FROM x\nRUN <<-EOF\n\tCOPY secret /nowhere\n\tEOF\nRUN true\n",
		"a COPY inside a RUN line": "FROM x\nRUN echo COPY secret /nowhere\n",
		"CRLF line ends":           "FROM x\r\nRUN true\r\n",
		"a byte order mark":        "\xef\xbb\xbfFROM x\n",
	} {
		t.Run(name, func(t *testing.T) {
			if err := docker.CheckDockerfile([]byte(text)); err != nil {
				t.Fatal(err)
			}
		})
	}
}

func TestADockerfileWithoutFromIsInvalid(t *testing.T) {
	for _, text := range []string{"", "\n\n", "# only a comment\n", "RUN true\n", "ARG A=1\n"} {
		f := failure(t, docker.CheckDockerfile([]byte(text)))
		if f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, "FROM") {
			t.Errorf("%q: got %v, want invalid input naming FROM", text, f)
		}
	}
}

func TestTheEscapeDirectiveChangesWhereALineContinues(t *testing.T) {
	// With a backtick escape, a trailing backslash does not continue the line, so the COPY on
	// the next line is an instruction of its own.
	text := "# escape=`\nFROM x\nRUN echo a \\\nCOPY f /f\n"
	if f := failure(t, docker.CheckDockerfile([]byte(text))); !strings.Contains(f.Message, "line 4") {
		t.Errorf("got %v, want the COPY on line 4", f)
	}
	// With the default escape the backslash continues the RUN, and the COPY is part of it.
	if err := docker.CheckDockerfile([]byte("FROM x\nRUN echo a \\\nCOPY f /f\n")); err != nil {
		t.Errorf("a continued RUN: %v", err)
	}
}

func TestTheLineOfAnInstructionIsWhereItStarts(t *testing.T) {
	text := "FROM x\n\n# c\nRUN a \\\n  b\nCOPY f /f\n"
	if f := failure(t, docker.CheckDockerfile([]byte(text))); !strings.Contains(f.Message, "line 6") {
		t.Errorf("got %v, want line 6", f)
	}
}

// A Dockerfile that is accepted stays accepted when the text around its instructions changes in
// ways docker ignores, and a refused one stays refused.
func TestTheVerdictIgnoresWhatDockerIgnores(t *testing.T) {
	texts := []string{realisticDockerfile, "FROM x\nCOPY f /f\n", "RUN true\n", "FROM x\nRUN --mount=source=a,target=/a true\n", "FROM x\nCOPY <<EOF /f\nCOPY q /q\nEOF\n"}
	rng := rand.New(rand.NewPCG(1, 2))
	for _, text := range texts {
		want := verdict(text)
		for range 50 {
			if got := verdict(mutate(rng, text)); got != want {
				t.Fatalf("verdict changed from %q to %q after an ignorable change to:\n%s", want, got, text)
			}
		}
	}
}

func verdict(text string) string {
	err := docker.CheckDockerfile([]byte(text))
	if err == nil {
		return "ok"
	}
	var f *core.Failure
	if errors.As(err, &f) {
		return string(f.Kind)
	}
	return "not a failure: " + err.Error()
}

// mutate changes text in ways that cannot change what a Dockerfile means: comment and blank lines
// between instructions, CRLF line ends, and a byte order mark.
func mutate(rng *rand.Rand, text string) string {
	lines := strings.Split(strings.TrimSuffix(text, "\n"), "\n")
	var out []string
	inContinuation := false
	for _, l := range lines {
		// Blank and comment lines go only between instructions, and not into a here-document,
		// where they would be part of the body.
		if !inContinuation && !strings.Contains(text, "<<") {
			for rng.IntN(3) == 0 {
				out = append(out, []string{"", "  ", "# a comment", "   # COPY nothing /here"}[rng.IntN(4)])
			}
		}
		out = append(out, l)
		inContinuation = strings.HasSuffix(strings.TrimRight(l, " \t"), "\\")
	}
	sep := "\n"
	if rng.IntN(2) == 0 {
		sep = "\r\n"
	}
	res := strings.Join(out, sep) + sep
	if rng.IntN(2) == 0 {
		res = "\xef\xbb\xbf" + res
	}
	return res
}

func FuzzCheckDockerfile(f *testing.F) {
	for _, seed := range []string{
		realisticDockerfile, "", "FROM x", "FROM x\nCOPY f /f", "RUN <<EOF", "RUN <<'EOF'\nx", "COPY [", `COPY ["a"]`,
		"# escape=`\nFROM x\nRUN a `\nCOPY b /b", "FROM x\nRUN --mount", "FROM x\nRUN --mount=type", "\\\n\\\n", "FROM x\nADD <<-E /f\n\tbody\n\tE",
		"FROM\x00x\nCOPY \xff /f",
	} {
		f.Add([]byte(seed))
	}
	f.Fuzz(func(t *testing.T, text []byte) {
		err := docker.CheckDockerfile(text)
		again := docker.CheckDockerfile(text)
		if (err == nil) != (again == nil) {
			t.Fatalf("the verdict is not a function of the text")
		}
		if err != nil {
			fl := failure(t, err)
			if fl.Kind != core.FailureUnavailable && fl.Kind != core.FailureInvalidInput {
				t.Fatalf("kind %q is neither unavailable nor invalid input", fl.Kind)
			}
			if !strings.HasPrefix(fl.Message, "docker: ") {
				t.Fatalf("message %q does not say whose it is", fl.Message)
			}
			return
		}
		// An accepted Dockerfile has an instruction to start from.
		if !bytes.Contains(bytes.ToUpper(text), []byte("FROM")) {
			t.Fatalf("accepted a Dockerfile with no FROM:\n%q", text)
		}
	})
}
