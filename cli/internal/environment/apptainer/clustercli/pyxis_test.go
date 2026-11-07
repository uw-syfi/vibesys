package clustercli_test

import (
	"bytes"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/quick"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer"
	"github.com/uw-syfi/vibesys/cli/internal/environment/apptainer/clustercli"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
)

// pyxisNode is the node of the job 77 of a cluster whose boxes run under the pyxis runtime, and the
// directory that is the node's file system.
func pyxisNode(t *testing.T, sh shell) (apptainer.Node, string) {
	t.Helper()
	nodeRoot := t.TempDir()
	if err := os.WriteFile(filepath.Join(sh.bin, "node_root"), []byte(nodeRoot), 0o644); err != nil {
		t.Fatal(err)
	}
	c, err := clustercli.New(clustercli.Config{Shell: sh, Timers: envtest.NewTimers(), Submit: []string{"sbatch"}, Runtime: apptainer.RuntimePyxis})
	if err != nil {
		t.Fatal(err)
	}
	return c.Node("77"), nodeRoot
}

// Under the pyxis runtime a box's process is a step of a container of the squashfs: read-only,
// with the box's mounts and its home at /tmp, made on the node first, its directory as the working
// directory, its variables over the image's, and none of the user's home, root or the image's
// entrypoint. Its standard error is merged into its output, and it ends with the command's code.
func TestUnderPyxisABoxsProcessRunsInAContainerOfTheSquashfs(t *testing.T) {
	sh := newShell(t)
	n, nodeRoot := pyxisNode(t, sh)
	if err := os.MkdirAll(filepath.Join(nodeRoot, "work"), 0o755); err != nil {
		t.Fatal(err)
	}
	box := apptainer.Box{
		Name: "vibesys-cli-0a", Image: "/shared/images/x.sqsh", Dir: "/work", Env: map[string]string{"GREETING": "a, b"},
		Mounts: []docker.Mount{{Host: "/shared/run", Container: "/work"}, {Host: "/shared/model", Container: "/model", ReadOnly: true}},
		ROCm:   true,
	}
	var out bytes.Buffer
	p, err := n.Start(box, []string{"/bin/sh", "-c", `echo "$GREETING $VIBESYS_BOX $(basename "$PWD")"; echo oops >&2; exit 3`}, &out)
	if err != nil {
		t.Fatal(err)
	}
	if e := wait(t, p); e != (environment.Exit{Code: 3}) {
		t.Fatalf("exit %+v", e)
	}
	if got := out.String(); got != "a, b vibesys-cli-0a work\noops\n" {
		t.Errorf("output %q", got)
	}
	home := "/tmp/vibesys-job-77/home-vibesys-cli-0a"
	if info, err := os.Stat(filepath.Join(nodeRoot, home)); err != nil || !info.IsDir() {
		t.Errorf("the box's home %s was not made on the node: %v", home, err)
	}
	log, _ := os.ReadFile(filepath.Join(sh.bin, "pyxis.log"))
	for _, want := range []string{
		"--container-image=/shared/images/x.sqsh", "--container-readonly",
		"--container-mounts=" + home + ":/tmp,/shared/run:/work,/shared/model:/model:ro",
		"--container-env=GREETING,VIBESYS_BOX", "--no-container-mount-home", "--no-container-remap-root",
		"--no-container-entrypoint", "--container-workdir=/work",
	} {
		if !strings.Contains(string(log), want+" ") && !strings.HasSuffix(strings.TrimSpace(string(log)), want) {
			t.Errorf("srun's pyxis options %q lack %q", log, want)
		}
	}
	if strings.Contains(string(log), "--container-name") {
		t.Errorf("a box's container is named, so it outlives its step: %q", log)
	}
}

// Under the pyxis runtime a box's process gets exactly its command's arguments and its box's
// variables, whatever they hold (spaces, quotes, commas, newlines, "--", what looks like an option
// or an assignment), and nothing of the login node's environment but PATH.
func TestUnderPyxisAProcessGetsItsArgumentsAndVariablesAsTheyAre(t *testing.T) {
	sh := newShell(t)
	sh.env = []string{"LOGIN_NODE_SECRET=leaked"}
	n, _ := pyxisNode(t, sh)
	pieces := []string{"a", " ", "--", "-x", "K=V", "'", `"`, "$HOME", "`id`", ",", ":", "\\", "\n", "é", "*"}
	word := func(rnd *rand.Rand) string {
		var b strings.Builder
		for range rnd.IntN(4) {
			b.WriteString(pieces[rnd.IntN(len(pieces))])
		}
		return b.String()
	}
	err := quick.Check(func(seed uint64) bool {
		rnd := rand.New(rand.NewPCG(seed, 2))
		args := []string{"--"}
		for range rnd.IntN(4) {
			args = append(args, word(rnd))
		}
		env := map[string]string{}
		for i := range rnd.IntN(3) {
			env[fmt.Sprintf("V_%d", i)] = word(rnd)
		}
		argv := append([]string{"/bin/sh", "-c", `printf '%s\0' "$@"; printf '==env==\0'; env -0 | LC_ALL=C sort -z`, "vs-test"}, args...)
		var out bytes.Buffer
		p, err := n.Start(apptainer.Box{Name: "b", Image: "/shared/x.sqsh", Env: env}, argv, &out)
		if err != nil {
			t.Fatal(err)
		}
		if e := wait(t, p); e != (environment.Exit{}) {
			t.Logf("exit %+v: %q", e, out.String())
			return false
		}
		got, vars, _ := strings.Cut(out.String(), "==env==\x00")
		if want := strings.Join(args, "\x00") + "\x00"; got != want {
			t.Logf("arguments %q, want %q", got, want)
			return false
		}
		have := map[string]string{}
		for _, kv := range strings.Split(strings.TrimSuffix(vars, "\x00"), "\x00") {
			k, v, _ := strings.Cut(kv, "=")
			have[k] = v
		}
		for k, v := range env {
			if have[k] != v {
				t.Logf("variable %s is %q, want %q", k, have[k], v)
				return false
			}
		}
		if have[apptainer.EnvBox] != "b" || have["LOGIN_NODE_SECRET"] != "" {
			t.Logf("variables %q", have)
			return false
		}
		return true
	}, &quick.Config{MaxCount: 25})
	if err != nil {
		t.Error(err)
	}
}

// A box whose step under pyxis could not make its home never starts its command, and is lost.
func TestUnderPyxisAStepThatCouldNotStartIsLost(t *testing.T) {
	sh := newShell(t)
	sh.env = []string{"SRUN_FAIL=1"}
	n, _ := pyxisNode(t, sh)
	p, err := n.Start(apptainer.Box{Name: "b", Image: "/shared/x.sqsh"}, []string{"true"}, &bytes.Buffer{})
	if err != nil {
		t.Fatal(err)
	}
	if e := wait(t, p); e.Err == nil || !strings.Contains(e.Err.Error(), "did not start") {
		t.Errorf("exit %+v, want lost", e)
	}
	for _, name := range []string{"x,y", "x=y", ""} {
		if _, err := n.Start(apptainer.Box{Name: "b", Image: "/shared/x.sqsh", Env: map[string]string{name: "v"}}, []string{"true"}, &bytes.Buffer{}); err == nil {
			t.Errorf("a variable named %q was passed to Pyxis", name)
		}
	}
	if _, err := n.Start(apptainer.Box{Name: "b", Image: "/shared/x.sqsh", Mounts: []docker.Mount{{Host: "/a,b", Container: "/c"}}}, []string{"true"}, &bytes.Buffer{}); err == nil {
		t.Error("a mount with a comma was passed to Pyxis")
	}
}

// fakeSqfstar makes its squashfs of the tar archive on its input: the archive itself, as the fake
// unsquashfs reads one. With $SQFSTAR_FAIL set it fails after reading its input.
const fakeSqfstar = `#!/bin/sh
for a do out="$a"; done
if [ "$1" = -version ]; then echo "sqfstar version 4.6.1 (fake)"; exit 0; fi
cat > "$out"
if [ -n "${SQFSTAR_FAIL:-}" ]; then echo "FATAL ERROR: $SQFSTAR_FAIL" >&2; exit 1; fi
`

// fakeUnsquashfs prints a file of a fake squashfs with -cat.
const fakeUnsquashfs = `#!/bin/sh
exec tar -xOf "$2" "$3"
`

func squashShell(t *testing.T) shell {
	t.Helper()
	sh := newShell(t)
	for name, text := range map[string]string{"sqfstar": fakeSqfstar, "unsquashfs": fakeUnsquashfs} {
		if err := os.WriteFile(filepath.Join(sh.bin, name), []byte(text), 0o755); err != nil {
			t.Fatal(err)
		}
	}
	sh.env = append(sh.env, "TMPDIR="+t.TempDir())
	return sh
}

// rootfsOf is a root file system's stream whose marker holds the content, cut to n bytes when n is
// not negative.
func rootfsOf(t *testing.T, content string, n int) []byte {
	t.Helper()
	var b bytes.Buffer
	if err := apptainer.RootFS(bytes.NewReader(nil), []string{"A=1"}, content, &b); err != nil {
		t.Fatal(err)
	}
	if n >= 0 {
		return b.Bytes()[:n]
	}
	return b.Bytes()
}

// zeros never ends.
type zeros struct{}

func (zeros) Read(p []byte) (int, error) { clear(p); return len(p), nil }

// A squashfs is made of the whole stream and renamed into place, saying what made it; one of a
// stream cut short, or that sqfstar failed, is never there and leaves nothing beside it; and one
// already there is kept without reading the stream to its end.
func TestASquashfsIsThereWholeOrNotAtAll(t *testing.T) {
	sh := squashShell(t)
	c := newCluster(t, sh)
	dir := t.TempDir()
	p := filepath.Join(dir, "images", "abc.sqsh")
	whole := rootfsOf(t, "sha256:abc", -1)
	for name, stream := range map[string][]byte{
		"cut short between entries": whole[:len(whole)-3*512-1024],
		"cut short in an entry":     whole[:700],
	} {
		if _, err := c.Squash(ctx(t), p, "sha256:abc", bytes.NewReader(stream)); err == nil {
			t.Errorf("%s: a squashfs was made", name)
		}
		if names := entries(t, filepath.Join(dir, "images")); len(names) != 0 {
			t.Errorf("%s: left %v", name, names)
		}
	}
	failing := sh
	failing.env = append(slices.Clone(sh.env), "SQFSTAR_FAIL=out of space")
	if _, err := newCluster(t, failing).Squash(ctx(t), p, "sha256:abc", bytes.NewReader(whole)); err == nil || !strings.Contains(err.Error(), "out of space") {
		t.Errorf("a failing sqfstar: %v", err)
	}
	by, err := c.Squash(ctx(t), p, "sha256:abc", bytes.NewReader(whole))
	if err != nil || by != "sqfstar version 4.6.1 (fake)" {
		t.Fatalf("made by %q, %v", by, err)
	}
	if got, _ := os.ReadFile(p); !bytes.Equal(got, whole) {
		t.Error("the squashfs is not of the whole stream")
	}
	if names := entries(t, filepath.Join(dir, "images")); !slices.Equal(names, []string{"abc.sqsh"}) {
		t.Errorf("left %v", names)
	}
	if _, err := c.Squash(ctx(t), p, "sha256:abc", zeros{}); err != nil {
		t.Errorf("a squashfs already there: %v", err)
	}
	if got, _ := os.ReadFile(p); !bytes.Equal(got, whole) {
		t.Error("a squashfs already there was replaced")
	}
}
