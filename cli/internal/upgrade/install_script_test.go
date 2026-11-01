package upgrade_test

// Tests of cli/install.sh against a release served from local files. The script is the other
// reader of the release layout this package defines, so its tests build releases with the
// package's own asset names: a disagreement between the two fails here.

import (
	"bytes"
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"slices"
	"strings"
	"testing"
	"testing/quick"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/upgrade"
	"github.com/uw-syfi/vibesys/cli/internal/upgrade/upgradetest"
)

// hangGuard only ends a script that never returns; no result depends on it.
const hangGuard = 2 * time.Minute

// cliDir is the cli/ directory, where install.sh and build_release.sh live.
func cliDir(t *testing.T) string {
	t.Helper()
	dir, err := filepath.Abs("../..")
	must(t, err)
	if _, err := os.Stat(filepath.Join(dir, "install.sh")); err != nil {
		t.Fatalf("install.sh not found under %s: %v", dir, err)
	}
	return dir
}

// shellBinary is a stand-in for a release binary: it runs and says which asset it is.
func shellBinary(label string) []byte {
	return []byte("#!/bin/sh\necho 'vibesys-cli " + label + "'\n")
}

// releaseFor returns a release of the version whose binary for each platform says which asset it
// is, so a test can tell which one was installed.
func releaseFor(version string) *upgradetest.Release {
	binaries := map[upgrade.Platform][]byte{}
	for _, platform := range upgrade.Platforms {
		name, _ := upgrade.AssetName(platform)
		binaries[platform] = shellBinary(version + " " + name)
	}
	return upgradetest.New(version, binaries)
}

// serve writes the release under <root>/<tag>/ for each tag and returns a file:// releases URL.
func serve(t *testing.T, release *upgradetest.Release, tags ...string) string {
	t.Helper()
	root := t.TempDir()
	for _, tag := range tags {
		must(t, os.Mkdir(filepath.Join(root, tag), 0o755))
		for name, content := range release.Files() {
			must(t, os.WriteFile(filepath.Join(root, tag, name), content, 0o644))
		}
	}
	return "file://" + root
}

// machine is what `uname` and `sysctl` report to the script.
type machine struct{ system, arch, sysctlArm64 string }

// script is one run of install.sh.
type script struct {
	shell      string
	url        string
	installDir string
	home       string
	version    string
	machine    machine
	stdin      []byte // when set, the script is piped in, as `curl | sh` does
	path       string // appended to the fake-tool directory; empty means the real PATH
}

type run struct {
	out, errOut string
	ok          bool
	tmp         string // TMPDIR of the run, which the script must leave empty
}

func (s script) run(t *testing.T) run {
	t.Helper()
	tools := t.TempDir()
	write := func(name, body string) {
		must(t, os.WriteFile(filepath.Join(tools, name), []byte("#!/bin/sh\n"+body), 0o755))
	}
	write("uname", fmt.Sprintf("case \"$1\" in -s) echo '%s';; -m) echo '%s';; esac\n", s.machine.system, s.machine.arch))
	if s.machine.sysctlArm64 != "" {
		write("sysctl", fmt.Sprintf("echo %s\n", s.machine.sysctlArm64))
	}
	tmp := t.TempDir()
	path := os.Getenv("PATH")
	if s.path != "" {
		path = s.path
	}
	env := []string{"PATH=" + tools + ":" + path, "TMPDIR=" + tmp, "VIBESYS_CLI_RELEASES_URL=" + s.url}
	if s.home != "" {
		env = append(env, "HOME="+s.home)
	}
	if s.installDir != "" {
		env = append(env, "VIBESYS_CLI_INSTALL_DIR="+s.installDir)
	}
	if s.version != "" {
		env = append(env, "VIBESYS_CLI_VERSION="+s.version)
	}
	ctx, cancel := context.WithTimeout(context.Background(), hangGuard)
	defer cancel()
	shell := s.shell
	if shell == "" {
		shell = "sh"
	}
	var command *exec.Cmd
	if s.stdin != nil {
		command = exec.CommandContext(ctx, shell, "-s")
		command.Stdin = bytes.NewReader(s.stdin)
	} else {
		command = exec.CommandContext(ctx, shell, filepath.Join(cliDir(t), "install.sh"))
	}
	command.Env = env
	var stdout, stderr bytes.Buffer
	command.Stdout, command.Stderr = &stdout, &stderr
	err := command.Run()
	return run{out: stdout.String(), errOut: stderr.String(), ok: err == nil, tmp: tmp}
}

// shells are the POSIX shells present on this machine; the script must work under each.
func shells(t *testing.T) []string {
	t.Helper()
	var found []string
	for _, name := range []string{"sh", "dash", "bash"} {
		if _, err := exec.LookPath(name); err == nil {
			found = append(found, name)
		}
	}
	if !slices.Contains(found, "sh") {
		t.Fatal("no sh on PATH")
	}
	for _, tool := range []string{"curl", "awk", "mktemp"} {
		if _, err := exec.LookPath(tool); err != nil {
			t.Fatalf("install.sh's tests need %s on PATH: %v", tool, err)
		}
	}
	return found
}

func names(t *testing.T, dir string) []string {
	t.Helper()
	entries, err := os.ReadDir(dir)
	must(t, err)
	var found []string
	for _, entry := range entries {
		found = append(found, entry.Name())
	}
	return found
}

func (r run) requireClean(t *testing.T) {
	t.Helper()
	if left := names(t, r.tmp); len(left) != 0 {
		t.Errorf("the script left %v in its temporary directory", left)
	}
}

func TestInstallPicksTheBinaryForEveryMachine(t *testing.T) {
	machines := []struct {
		machine machine
		want    string
	}{
		{machine{"Linux", "x86_64", ""}, "linux_amd64"},
		{machine{"Linux", "amd64", ""}, "linux_amd64"},
		{machine{"Linux", "aarch64", ""}, "linux_arm64"},
		{machine{"Linux", "arm64", ""}, "linux_arm64"},
		{machine{"Darwin", "x86_64", ""}, "darwin_amd64"},
		{machine{"Darwin", "x86_64", "0"}, "darwin_amd64"},
		{machine{"Darwin", "arm64", ""}, "darwin_arm64"},
		// A shell running under Rosetta on an Apple Silicon Mac.
		{machine{"Darwin", "x86_64", "1"}, "darwin_arm64"},
	}
	url := serve(t, releaseFor("3.0.0"), "cli-latest")
	for _, shell := range shells(t) {
		for _, m := range machines {
			t.Run(shell+"/"+m.machine.system+"-"+m.machine.arch+"-"+m.machine.sysctlArm64, func(t *testing.T) {
				dir := filepath.Join(t.TempDir(), "nested", "bin")
				r := script{shell: shell, url: url, installDir: dir, machine: m.machine}.run(t)
				if !r.ok {
					t.Fatalf("install failed: %s", r.errOut)
				}
				if got := names(t, dir); !slices.Equal(got, []string{"vibesys-cli"}) {
					t.Fatalf("install directory holds %v, want only vibesys-cli", got)
				}
				info, err := os.Stat(filepath.Join(dir, "vibesys-cli"))
				must(t, err)
				if info.Mode().Perm() != 0o755 {
					t.Errorf("mode = %v, want 0755", info.Mode().Perm())
				}
				out, err := exec.Command(filepath.Join(dir, "vibesys-cli"), "--version").Output()
				must(t, err)
				if want := "vibesys-cli 3.0.0 vibesys-cli_" + m.want + "\n"; string(out) != want {
					t.Fatalf("installed binary says %q, want %q", out, want)
				}
				if !strings.Contains(r.out, "vibesys-cli 3.0.0") || !strings.Contains(r.out, filepath.Join(dir, "vibesys-cli")) {
					t.Errorf("output = %q, want the version and the path", r.out)
				}
				r.requireClean(t)
			})
		}
	}
}

func TestInstallRefusesAMachineWithoutABinary(t *testing.T) {
	url := serve(t, releaseFor("3.0.0"), "cli-latest")
	for _, m := range []machine{{"FreeBSD", "amd64", ""}, {"Linux", "riscv64", ""}, {"Linux", "i686", ""}, {"Darwin", "ppc", ""}, {"Windows_NT", "x86_64", ""}} {
		t.Run(m.system+"-"+m.arch, func(t *testing.T) {
			dir := filepath.Join(t.TempDir(), "bin")
			r := script{url: url, installDir: dir, machine: m}.run(t)
			if r.ok {
				t.Fatal("install succeeded")
			}
			if !strings.Contains(r.errOut, m.system) && !strings.Contains(r.errOut, m.arch) {
				t.Errorf("error %q names neither %s nor %s", r.errOut, m.system, m.arch)
			}
			if _, err := os.Stat(dir); err == nil {
				t.Error("the install directory was created")
			}
			r.requireClean(t)
		})
	}
}

func TestInstallOfAPinnedVersionReadsThatVersionsTag(t *testing.T) {
	root := t.TempDir()
	for tag, version := range map[string]string{"cli-latest": "9.0.0", "cli-v3.0.0": "3.0.0"} {
		url := serve(t, releaseFor(version), tag)
		must(t, os.Rename(strings.TrimPrefix(url, "file://")+"/"+tag, filepath.Join(root, tag)))
	}
	linux := machine{"Linux", "x86_64", ""}
	for _, pin := range []string{"3.0.0", "v3.0.0"} {
		dir := filepath.Join(t.TempDir(), "bin")
		r := script{url: "file://" + root, installDir: dir, version: pin, machine: linux}.run(t)
		if !r.ok || !strings.Contains(r.out, "vibesys-cli 3.0.0 ") {
			t.Fatalf("pin %q: ok = %v, output %q, error %q", pin, r.ok, r.out, r.errOut)
		}
	}
	dir := filepath.Join(t.TempDir(), "bin")
	if r := (script{url: "file://" + root, installDir: dir, version: "4.0.0", machine: linux}).run(t); r.ok || !strings.Contains(r.errOut, "cli-v4.0.0") {
		t.Fatalf("a version that was never released: ok = %v, error %q", r.ok, r.errOut)
	}
	for _, bad := range []string{"3.0.0; rm -rf /", "../x", "3 0"} {
		if r := (script{url: "file://" + root, installDir: dir, version: bad, machine: linux}).run(t); r.ok {
			t.Errorf("version %q was accepted", bad)
		}
	}
}

// installed is a directory holding an earlier install, to show a refused upgrade leaves it be.
func installed(t *testing.T) string {
	t.Helper()
	dir := filepath.Join(t.TempDir(), "bin")
	must(t, os.Mkdir(dir, 0o755))
	must(t, os.WriteFile(filepath.Join(dir, "vibesys-cli"), shellBinary("old"), 0o755))
	return dir
}

func requireOldInstall(t *testing.T, dir string) {
	t.Helper()
	if got := names(t, dir); !slices.Equal(got, []string{"vibesys-cli"}) {
		t.Errorf("install directory holds %v, want only vibesys-cli", got)
	}
	content, err := os.ReadFile(filepath.Join(dir, "vibesys-cli"))
	must(t, err)
	if !bytes.Equal(content, shellBinary("old")) {
		t.Errorf("the earlier install was replaced by %q", content)
	}
}

// A download that does not match the published checksum, cut short at any byte or with any byte
// changed, installs nothing and leaves an earlier install as it was.
func TestInstallRefusesAnyDamagedDownload(t *testing.T) {
	asset, _ := upgrade.AssetName(upgrade.Platform{OS: "linux", Arch: "amd64"})
	original := releaseFor("3.0.0").Files()[asset]
	damaged := func(t *testing.T, content []byte) {
		t.Helper()
		release := releaseFor("3.0.0")
		url := serve(t, release, "cli-latest")
		// Replace the binary after the checksums were written for the original.
		must(t, os.WriteFile(strings.TrimPrefix(url, "file://")+"/cli-latest/"+asset, content, 0o644))
		dir := installed(t)
		r := script{url: url, installDir: dir, machine: machine{"Linux", "x86_64", ""}}.run(t)
		if r.ok || !strings.Contains(r.errOut, "checksum") {
			t.Fatalf("content %q: ok = %v, error %q", content, r.ok, r.errOut)
		}
		requireOldInstall(t, dir)
		r.requireClean(t)
	}
	property := func(cut uint, at uint, flip byte) bool {
		damaged(t, original[:cut%uint(len(original))])
		if flip != 0 {
			changed := bytes.Clone(original)
			changed[at%uint(len(changed))] ^= flip
			damaged(t, changed)
		}
		return !t.Failed()
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 10}); err != nil {
		t.Fatal(err)
	}
	damaged(t, nil)
}

func TestInstallRefusesChecksumsItCannotTrust(t *testing.T) {
	asset, _ := upgrade.AssetName(upgrade.Platform{OS: "linux", Arch: "amd64"})
	good := string(releaseFor("3.0.0").Files()[upgrade.ChecksumsAsset])
	var line string
	for _, l := range strings.Split(good, "\n") {
		if strings.HasSuffix(l, asset) {
			line = l
		}
	}
	zero := strings.Repeat("0", 64)
	for label, text := range map[string]string{
		"no entry for the asset": strings.ReplaceAll(good, line+"\n", ""),
		"entry listed twice":     good + line + "\n",
		"a wrong digest":         zero + "  " + asset + "\n",
		"a short digest":         strings.Repeat("a", 63) + "  " + asset + "\n",
		"an html error page":     "<html>Not Found</html>\n",
		"an empty file":          "",
	} {
		t.Run(label, func(t *testing.T) {
			url := serve(t, releaseFor("3.0.0"), "cli-latest")
			must(t, os.WriteFile(strings.TrimPrefix(url, "file://")+"/cli-latest/checksums.txt", []byte(text), 0o644))
			dir := installed(t)
			r := script{url: url, installDir: dir, machine: machine{"Linux", "x86_64", ""}}.run(t)
			if r.ok {
				t.Fatalf("install succeeded; output %q", r.out)
			}
			requireOldInstall(t, dir)
			r.requireClean(t)
		})
	}
}

func TestInstallAcceptsTheBinaryModeSpellingOfAChecksum(t *testing.T) {
	url := serve(t, releaseFor("3.0.0"), "cli-latest")
	path := strings.TrimPrefix(url, "file://") + "/cli-latest/checksums.txt"
	text, err := os.ReadFile(path)
	must(t, err)
	must(t, os.WriteFile(path, bytes.ReplaceAll(text, []byte("  vibesys-cli_"), []byte(" *vibesys-cli_")), 0o644))
	r := script{url: url, installDir: filepath.Join(t.TempDir(), "bin"), machine: machine{"Linux", "x86_64", ""}}.run(t)
	if !r.ok {
		t.Fatalf("install failed: %s", r.errOut)
	}
}

func TestInstallReplacesAnEarlierInstallOnlyWithABinaryThatRuns(t *testing.T) {
	// A binary with the right checksum that cannot run on this machine.
	binaries := map[upgrade.Platform][]byte{{OS: "linux", Arch: "amd64"}: []byte("#!/bin/sh\nexit 3\n")}
	url := serve(t, upgradetest.New("3.0.0", binaries), "cli-latest")
	dir := installed(t)
	r := script{url: url, installDir: dir, machine: machine{"Linux", "x86_64", ""}}.run(t)
	if r.ok || !strings.Contains(r.errOut, "does not run") {
		t.Fatalf("ok = %v, error %q", r.ok, r.errOut)
	}
	requireOldInstall(t, dir)
	r.requireClean(t)

	good := serve(t, releaseFor("3.0.0"), "cli-latest")
	if r := (script{url: good, installDir: dir, machine: machine{"Linux", "x86_64", ""}}).run(t); !r.ok {
		t.Fatalf("install over the earlier one failed: %s", r.errOut)
	}
	out, err := exec.Command(filepath.Join(dir, "vibesys-cli"), "--version").Output()
	must(t, err)
	if !strings.HasPrefix(string(out), "vibesys-cli 3.0.0") {
		t.Fatalf("installed binary says %q", out)
	}
}

func TestInstallDefaultsToTheHomeLocalBinAndSaysWhenItIsNotOnPath(t *testing.T) {
	url := serve(t, releaseFor("3.0.0"), "cli-latest")
	home := t.TempDir()
	r := script{url: url, home: home, machine: machine{"Linux", "x86_64", ""}}.run(t)
	if !r.ok {
		t.Fatalf("install failed: %s", r.errOut)
	}
	if _, err := os.Stat(filepath.Join(home, ".local", "bin", "vibesys-cli")); err != nil {
		t.Fatal(err)
	}
	if !strings.Contains(r.out, "PATH") {
		t.Errorf("output %q does not tell the user to add the directory to PATH", r.out)
	}

	onPath := script{url: url, home: home, machine: machine{"Linux", "x86_64", ""}, path: filepath.Join(home, ".local", "bin") + ":" + os.Getenv("PATH")}.run(t)
	if !onPath.ok || strings.Contains(onPath.out, "PATH") {
		t.Errorf("a directory already on PATH: ok = %v, output %q", onPath.ok, onPath.out)
	}
}

func TestInstallWithNoHomeAndNoDirectoryAsksForOne(t *testing.T) {
	url := serve(t, releaseFor("3.0.0"), "cli-latest")
	r := script{url: url, machine: machine{"Linux", "x86_64", ""}}.run(t)
	if r.ok || !strings.Contains(r.errOut, "VIBESYS_CLI_INSTALL_DIR") {
		t.Fatalf("ok = %v, error %q", r.ok, r.errOut)
	}
}

// Piped into a shell, as `curl ... | sh` runs it, the script installs.
func TestInstallRunsFromAPipe(t *testing.T) {
	source, err := os.ReadFile(filepath.Join(cliDir(t), "install.sh"))
	must(t, err)
	dir := filepath.Join(t.TempDir(), "bin")
	r := script{url: serve(t, releaseFor("3.0.0"), "cli-latest"), installDir: dir, machine: machine{"Linux", "x86_64", ""}, stdin: source}.run(t)
	if !r.ok {
		t.Fatalf("install failed: %s", r.errOut)
	}
	if got := names(t, dir); !slices.Equal(got, []string{"vibesys-cli"}) {
		t.Fatalf("install directory holds %v", got)
	}
}

// A download of the script that ends anywhere before its last line defines functions and runs
// nothing, so a connection that drops mid-script installs nothing.
func TestAScriptCutShortInstallsNothing(t *testing.T) {
	source, err := os.ReadFile(filepath.Join(cliDir(t), "install.sh"))
	must(t, err)
	lastLine := bytes.LastIndex(bytes.TrimRight(source, "\n"), []byte("\n")) + 1
	if !bytes.HasPrefix(source[lastLine:], []byte("main ")) {
		t.Fatalf("the script's last line is %q, want the call to main", source[lastLine:])
	}
	url := serve(t, releaseFor("3.0.0"), "cli-latest")
	property := func(cut uint) bool {
		dir := filepath.Join(t.TempDir(), "bin")
		script{url: url, installDir: dir, machine: machine{"Linux", "x86_64", ""}, stdin: source[:cut%uint(lastLine+1)]}.run(t)
		_, err := os.Stat(dir)
		return err != nil
	}
	if err := quick.Check(property, &quick.Config{MaxCount: 25}); err != nil {
		t.Fatal(err)
	}
}
