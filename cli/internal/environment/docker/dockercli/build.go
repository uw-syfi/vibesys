package dockercli

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"io"
	"math"
	"os/exec"
	"strconv"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// tailLines is how many of the last lines of a failed build its error carries.
const tailLines = 25

// maxError bounds the end of a build's log that an error carries.
const maxError = 8 << 10

// maxLine bounds the part of a line the tail keeps, so that a build that prints without ever
// ending a line cannot use all memory. The end of an over-long line is kept.
const maxLine = 64 << 10

// Build implements docker.Engine. The build context is passed as a tar on standard input, so the
// build can see no file but the context's.
func (e *Engine) Build(ctx context.Context, spec docker.BuildSpec, log io.Writer) error {
	if !tagPattern.MatchString(spec.Tag) || len(spec.Context) == 0 {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: a build needs a tag (got %q) and a build context", spec.Tag)}
	}
	args := []string{"build", "--progress=plain", "--tag", spec.Tag}
	for _, k := range sortedKeys(spec.Labels) {
		if err := checkLabel(k, spec.Labels[k]); err != nil {
			return err
		}
		args = append(args, "--label", k+"="+spec.Labels[k])
	}
	args = append(args, "-")
	cmd := e.command(ctx, &Allocation{Name: BuildName(spec.Tag), Scope: spec.Labels[docker.LabelScope], Build: true}, args...)
	cmd.Stdin = bytes.NewReader(spec.Context)
	out := &tail{w: log}
	// One writer for both streams: the build's progress is one ordered log.
	cmd.Stdout, cmd.Stderr = out, out
	err := cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exit *exec.ExitError
	if !asExit(err, &exit) {
		return unavailable("cannot run %q to build: %v; %s", e.program, err, hint)
	}
	if e.shell.Lost(exit.ExitCode()) {
		return unavailable("the build of the task's image on %s ended with the transport's own exit code %d (a lost connection, or a command that exited with it); the end of the build's output:\n%s", e.shell.Where(), exit.ExitCode(), out.last())
	}
	return unavailable("building the task's image failed (exit %d); the end of the build's output:\n%s", exit.ExitCode(), out.last())
}

// Save writes the image as a docker archive (docker save) to w, streaming: an image is as large as
// it is, so no bound is put on the copy but the caller's.
func (e *Engine) Save(ctx context.Context, image string, w io.Writer) error {
	if image == "" || strings.HasPrefix(image, "-") {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", image)}
	}
	cmd := e.command(ctx, nil, "save", image)
	cmd.Stdout = w
	errs := &tail{w: io.Discard}
	cmd.Stderr = errs
	err := cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exit *exec.ExitError
	if !asExit(err, &exit) {
		return unavailable("cannot run %q to save the image %s: %v; %s", e.program, image, err, hint)
	}
	return unavailable("docker save %s failed (exit %d): %s", image, exit.ExitCode(), clip(errs.last()))
}

// Export writes the files of a container of the image to w, as `docker export` does, streaming: a
// container is created of it (never started, with a command of its own, since an image may have
// none), exported, and removed whatever happens, so no bound is put on the copy but the caller's.
func (e *Engine) Export(ctx context.Context, image string, w io.Writer) error {
	if image == "" || strings.HasPrefix(image, "-") {
		return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", image)}
	}
	res, err := e.call(ctx, "create", "create", "--label", docker.Owner+".export=1", "--entrypoint", "", image, "/")
	if err != nil {
		return err
	}
	if res.code != 0 {
		return failed("create", res)
	}
	id := strings.TrimSpace(res.stdout)
	if id == "" || strings.ContainsAny(id, " \n") || strings.HasPrefix(id, "-") {
		return unavailable("docker create printed %q, which is not a container's id", clip(res.stdout))
	}
	defer func() {
		// The container holds nothing but the image's files: removing it may outlive the caller.
		_, _ = e.call(context.WithoutCancel(ctx), "rm", "rm", "--force", id)
	}()
	cmd := e.command(ctx, nil, "export", id)
	cmd.Stdout = w
	errs := &tail{w: io.Discard}
	cmd.Stderr = errs
	err = cmd.Run()
	if err == nil {
		return nil
	}
	if ctx.Err() != nil {
		return ctx.Err()
	}
	var exit *exec.ExitError
	if !asExit(err, &exit) {
		return unavailable("cannot run %q to export the image %s: %v; %s", e.program, image, err, hint)
	}
	return unavailable("docker export of a container of %s failed (exit %d): %s", image, exit.ExitCode(), clip(errs.last()))
}

// Env returns the image's environment, as `docker image inspect` says it, in its order.
func (e *Engine) Env(ctx context.Context, image string) ([]string, error) {
	if image == "" || strings.HasPrefix(image, "-") {
		return nil, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", image)}
	}
	res, err := e.call(ctx, "image inspect", "image", "inspect", "--format", "{{json .Config.Env}}", image)
	if err != nil {
		return nil, err
	}
	if res.code != 0 {
		return nil, failed("image inspect", res)
	}
	var env []string
	if err := json.Unmarshal([]byte(strings.TrimSpace(res.stdout)), &env); err != nil {
		return nil, unavailable("docker image inspect wrote what is not an environment: %v", err)
	}
	return env, nil
}

// Sizes returns the size of the image unpacked, as `docker image ls` says it, and the size of its
// content as the engine stores it, as `docker image inspect` says it. With the containerd image
// store, Docker's default, the content is the layers compressed, about what `docker save` writes;
// with the classic store it is the unpacked size again. The listing writes the unpacked size
// rounded to three significant digits, which is exact enough to size a conversion by.
func (e *Engine) Sizes(ctx context.Context, image string) (unpacked, stored int64, err error) {
	if image == "" || strings.HasPrefix(image, "-") {
		return 0, 0, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", image)}
	}
	res, err := e.call(ctx, "image inspect", "image", "inspect", "--format", "{{.Id}}\t{{.Size}}", image)
	if err != nil {
		return 0, 0, err
	}
	if res.code != 0 {
		return 0, 0, failed("image inspect", res)
	}
	id, size, _ := strings.Cut(strings.TrimSpace(res.stdout), "\t")
	stored, perr := strconv.ParseInt(size, 10, 64)
	if perr != nil || stored < 0 {
		return 0, 0, unavailable("docker image inspect says the image %s is %q bytes", image, size)
	}
	res, err = e.call(ctx, "image ls", "image", "ls", "--all", "--no-trunc", "--format", "{{.ID}}\t{{.Size}}")
	if err != nil {
		return 0, 0, err
	}
	if res.code != 0 {
		return 0, 0, failed("image ls", res)
	}
	for _, line := range strings.Split(res.stdout, "\n") {
		listed, size, found := strings.Cut(strings.TrimSpace(line), "\t")
		if found && listed == id {
			unpacked, err := ParseSize(size)
			return unpacked, stored, err
		}
	}
	return 0, 0, unavailable("docker image ls does not list the image %s", id)
}

// ParseSize reads a size as Docker writes it for a person, in decimal units: 437MB, 1.66GB.
func ParseSize(s string) (int64, error) {
	s = strings.TrimSpace(s)
	units := []struct {
		suffix string
		scale  float64
	}{{"PB", 1e15}, {"TB", 1e12}, {"GB", 1e9}, {"MB", 1e6}, {"kB", 1e3}, {"B", 1}}
	for _, u := range units {
		if num, ok := strings.CutSuffix(s, u.suffix); ok {
			v, err := strconv.ParseFloat(num, 64)
			n := math.Ceil(v * u.scale)
			// Not NaN, and below 2^63, the first float64 an int64 cannot hold.
			if err != nil || !(n >= 0 && n < math.MaxInt64) {
				break
			}
			return int64(n), nil
		}
	}
	return 0, unavailable("%q is not a size docker writes", s)
}

// BuildName is the unique name of a build of the tag, as an Allocator is given it: a build of one
// tag runs at most once at a time in one process, and its name says which image it builds.
func BuildName(tag string) string {
	return BuildPrefix + strings.ReplaceAll(strings.ReplaceAll(tag, ":", "-"), "/", "-")
}

// BuildPrefix starts the name of every build.
const BuildPrefix = docker.Owner + "-build-"

// tail passes a log on and remembers its last lines.
type tail struct {
	w io.Writer

	mu    sync.Mutex
	lines []string
	part  string
}

func (t *tail) Write(p []byte) (int, error) {
	t.mu.Lock()
	text := t.part + string(p)
	parts := strings.Split(text, "\n")
	t.part = clipEnd(parts[len(parts)-1], maxLine)
	for _, line := range parts[:len(parts)-1] {
		t.lines = append(t.lines, clipEnd(line, maxLine))
	}
	if extra := len(t.lines) - tailLines; extra > 0 {
		t.lines = t.lines[extra:]
	}
	t.mu.Unlock()
	// A log that cannot be written must not fail the build: the build's result is the image.
	_, _ = t.w.Write(p)
	return len(p), nil
}

// clipEnd keeps at most n bytes of the end of s.
func clipEnd(s string, n int) string {
	if len(s) > n {
		return s[len(s)-n:]
	}
	return s
}

func (t *tail) last() string {
	t.mu.Lock()
	defer t.mu.Unlock()
	lines := t.lines
	if t.part != "" {
		lines = append(append([]string(nil), lines...), t.part)
	}
	if extra := len(lines) - tailLines; extra > 0 {
		lines = lines[extra:]
	}
	return strings.ToValidUTF8(clipEnd(strings.Join(lines, "\n"), maxError), "")
}

// Content implements apptainer.Exporter: the digest of what `docker image inspect` says a
// container of the image runs (ContentDigest).
func (e *Engine) Content(ctx context.Context, image string) (string, error) {
	if image == "" || strings.HasPrefix(image, "-") {
		return "", &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("dockercli: %q is not an image reference", image)}
	}
	res, err := e.call(ctx, "image inspect", "image", "inspect", "--format", "{{json .}}", image)
	if err != nil {
		return "", err
	}
	if res.code != 0 {
		return "", failed("image inspect", res)
	}
	return ContentDigest([]byte(res.stdout))
}

// ContentDigest is the digest, as "sha256:" and 64 hexadecimal digits, of what an image's
// `docker image inspect` output (one image, as JSON) says a container of it runs: its Config, its
// platform (Os, Architecture, Variant), and its RootFS's layers, by diff ID and in order. What
// names this one build of the content (Id, RepoTags, RepoDigests, Created, Metadata, Descriptor)
// is left out: under the containerd image store the Id is the digest of the image's manifest or
// index, which BuildKit's attestations make differ from one build to the next of one content. The
// config is written canonically, its keys sorted, so the digest does not depend on how Docker
// orders them.
func ContentDigest(inspect []byte) (string, error) {
	var img struct {
		Config       json.RawMessage
		Os           string
		Architecture string
		Variant      string
		RootFS       *struct{ Layers []string }
	}
	if err := json.Unmarshal(inspect, &img); err != nil {
		return "", unavailable("docker image inspect wrote what is not an image: %v", err)
	}
	if img.RootFS == nil || img.Os == "" || img.Architecture == "" {
		return "", unavailable("docker image inspect wrote an image with no RootFS, Os or Architecture")
	}
	var config any
	if len(img.Config) > 0 {
		dec := json.NewDecoder(bytes.NewReader(img.Config))
		dec.UseNumber()
		if err := dec.Decode(&config); err != nil {
			return "", unavailable("docker image inspect wrote a config that is not JSON: %v", err)
		}
	}
	canonical, err := json.Marshal(struct {
		Config       any      `json:"config"`
		Os           string   `json:"os"`
		Architecture string   `json:"architecture"`
		Variant      string   `json:"variant"`
		Layers       []string `json:"layers"`
	}{config, img.Os, img.Architecture, img.Variant, img.RootFS.Layers})
	if err != nil {
		return "", unavailable("cannot write the content of an image: %v", err)
	}
	sum := sha256.Sum256(canonical)
	return "sha256:" + hex.EncodeToString(sum[:]), nil
}
