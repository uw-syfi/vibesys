package release_test

import (
	"errors"
	"io/fs"
	"math/rand/v2"
	"slices"
	"strings"
	"testing"
	"testing/fstest"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/release"
	"github.com/uw-syfi/vibesys/orchestrator/release/releasetest"
)

const dir = "/opt/vs"

// built lays out, in memory, every file a release holds once installed, and returns its manifest
// and the file system. Programs are executable, the rest are plain files.
func built(t *testing.T) (*pb.ReleaseManifest, fstest.MapFS) {
	t.Helper()
	m, err := release.NewManifest("1.0.0", "linux", "amd64")
	if err != nil {
		t.Fatal(err)
	}
	return m, releasetest.Tree(t, dir)
}

func open(t *testing.T, fsys fs.FS) *release.Release {
	t.Helper()
	r, err := release.Open(dir, fsys)
	if err != nil {
		t.Fatal(err)
	}
	return r
}

func TestACompleteReleaseVerifiesAtBothStages(t *testing.T) {
	_, fsys := built(t)
	r := open(t, fsys)
	for _, stage := range []release.Stage{release.Archived, release.Installed} {
		if err := r.Verify(stage); err != nil {
			t.Fatalf("stage %d: %v", stage, err)
		}
	}
}

// Whatever subset of a release's files is gone, verify fails and names exactly those files, each
// once; with none gone it passes. A release before install has no session holder environment, so
// that file is missing only at the installed stage.
func TestVerifyNamesEveryMissingFileAndNoOther(t *testing.T) {
	m, full := built(t)
	var files []string
	files = append(files, m.GetRequiredFiles()...)
	files = append(files, m.GetSessionHolderCommand()[0], m.GetSkills().GetRoot())
	slices.Sort(files)
	files = slices.Compact(files)

	rng := rand.New(rand.NewPCG(7, 11))
	for range 300 {
		gone := map[string]bool{}
		for _, f := range files {
			if f != m.GetSkills().GetRoot() && rng.IntN(3) == 0 {
				gone[f] = true
			}
		}
		fsys := fstest.MapFS{}
		for name, f := range full {
			fsys[name] = f
		}
		for f := range gone {
			delete(fsys, "opt/vs/"+f)
		}
		// A folder holding nothing else is gone with its last file, so the skills folder is missing
		// whenever its only listed file is.
		if gone[m.GetSkills().GetRoot()+"/.claude-plugin/marketplace.json"] {
			gone[m.GetSkills().GetRoot()] = true
		}
		r := open(t, fsys)
		for _, stage := range []release.Stage{release.Archived, release.Installed} {
			want := []string{}
			for f := range gone {
				if f == m.GetSessionHolderCommand()[0] && stage == release.Archived {
					continue
				}
				want = append(want, f)
			}
			slices.Sort(want)
			err := r.Verify(stage)
			if len(want) == 0 {
				if err != nil {
					t.Fatalf("nothing is missing at stage %d, got %v", stage, err)
				}
				continue
			}
			var incomplete *release.IncompleteError
			if !errors.As(err, &incomplete) {
				t.Fatalf("stage %d, missing %v: got %v", stage, want, err)
			}
			if !slices.Equal(incomplete.Missing, want) {
				t.Fatalf("stage %d: named %v, want exactly %v", stage, incomplete.Missing, want)
			}
			for _, f := range want {
				if !strings.Contains(err.Error(), f) {
					t.Fatalf("the message %q does not name %s", err, f)
				}
			}
		}
	}
}

func TestAProgramThatCannotRunIsMissing(t *testing.T) {
	m, fsys := built(t)
	fsys["opt/vs/"+m.GetHostProgram()] = &fstest.MapFile{Data: []byte("x"), Mode: 0o644}
	err := open(t, fsys).Verify(release.Archived)
	var incomplete *release.IncompleteError
	if !errors.As(err, &incomplete) || !slices.Equal(incomplete.Missing, []string{m.GetHostProgram()}) {
		t.Fatalf("got %v, want the host program named", err)
	}
}

func TestPartsAreResolvedWhenAsked(t *testing.T) {
	m, fsys := built(t)
	r := open(t, fsys)
	command, err := r.SessionHolderCommand()
	if err != nil || !slices.Equal(command, []string{"/opt/vs/sidecar/.venv/bin/python", "-m", "vs_sidecar"}) {
		t.Fatalf("got %q, %v", command, err)
	}
	if tool, err := r.JobTool(); err != nil || tool != "/opt/vs/bin/vibesys-cli" {
		t.Fatalf("got %q, %v", tool, err)
	}
	if root, err := r.SkillsRoot(); err != nil || root != "/opt/vs/marketplace" {
		t.Fatalf("got %q, %v", root, err)
	}
	// Repairing the install is seen by the next call on the same Release.
	delete(fsys, "opt/vs/"+m.GetJobTool())
	if _, err := r.JobTool(); err == nil || !strings.Contains(err.Error(), m.GetJobTool()) {
		t.Fatalf("a missing tool should be named, got %v", err)
	}
	fsys["opt/vs/"+m.GetJobTool()] = &fstest.MapFile{Mode: 0o755}
	if _, err := r.JobTool(); err != nil {
		t.Fatalf("a repaired tool should be found, got %v", err)
	}
}

func TestFindLooksBesideTheFoldersParentAndNotForAFolderCalledBin(t *testing.T) {
	_, fsys := built(t)
	for _, program := range []string{"/opt/vs/bin/vibesys-host", "/opt/vs/anything/vibesys-host"} {
		r, err := release.Find(program, fsys)
		if err != nil || r.Dir != dir {
			t.Fatalf("%s: got %v, %v", program, r, err)
		}
	}
	_, err := release.Find("/home/u/.local/bin/vibesys-host", fsys)
	var none *release.NotInstalledError
	if !errors.As(err, &none) || !strings.Contains(err.Error(), "/home/u/.local/release.json") {
		t.Fatalf("a developer's build is in no release and the error names what was looked for, got %v", err)
	}
	if _, err := release.Find("bin/vibesys-host", fsys); err == nil || !strings.Contains(err.Error(), "not absolute") {
		t.Fatalf("got %v", err)
	}
}

func TestAManifestIsRefusedForWhatItShouldNotSay(t *testing.T) {
	good := `{"version":"1.0.0","os":"linux","arch":"amd64","hostProgram":"bin/h","sessionHolderCommand":["py"],"skills":{"root":"s"},"jobTool":"bin/t","requiredFiles":["bin/h"]}`
	if _, err := release.Parse([]byte(good)); err != nil {
		t.Fatal(err)
	}
	bad := map[string]string{
		"an unknown field":          strings.Replace(good, `"os"`, `"colour":"red","os"`, 1),
		"a path out of the release": strings.Replace(good, `"bin/t"`, `"../t"`, 1),
		"an absolute path":          strings.Replace(good, `"bin/h","sess`, `"/bin/h","sess`, 1),
		"an escaping file":          strings.Replace(good, `["bin/h"]}`, `["a/../../b"]}`, 1),
		"an escaping holder":        strings.Replace(good, `["py"]`, `["../py"]`, 1),
		"a leading v":               strings.Replace(good, `"1.0.0"`, `"v1.0.0"`, 1),
		"a platform we do not ship": strings.Replace(good, `"amd64"`, `"mips"`, 1),
		"no skills":                 strings.Replace(good, `"skills":{"root":"s"},`, ``, 1),
		"no files":                  strings.Replace(good, `"requiredFiles":["bin/h"]`, `"requiredFiles":[]`, 1),
		"not json":                  "version: 1",
	}
	for name, text := range bad {
		if _, err := release.Parse([]byte(text)); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
}

func TestAManifestRoundTrips(t *testing.T) {
	m, _ := release.NewManifest("0.2.0-rc.1", "darwin", "arm64")
	data, err := release.Marshal(m)
	if err != nil {
		t.Fatal(err)
	}
	back, err := release.Parse(data)
	if err != nil || back.String() != m.String() {
		t.Fatalf("got %v, %v", back, err)
	}
}

func TestACorruptManifestIsNamedByPath(t *testing.T) {
	_, fsys := built(t)
	fsys["opt/vs/release.json"] = &fstest.MapFile{Data: []byte("{"), Mode: 0o644}
	_, err := release.Open(dir, fsys)
	var none *release.NotInstalledError
	if err == nil || errors.As(err, &none) || !strings.Contains(err.Error(), "/opt/vs/release.json") {
		t.Fatalf("a damaged manifest is not 'no release' and names its file, got %v", err)
	}
}

func TestThePlatformIsChecked(t *testing.T) {
	_, fsys := built(t)
	r := open(t, fsys)
	if err := r.CheckPlatform("linux", "amd64"); err != nil {
		t.Fatal(err)
	}
	if err := r.CheckPlatform("linux", "arm64"); err == nil || !strings.Contains(err.Error(), "linux/amd64") {
		t.Fatalf("got %v", err)
	}
}
