package core_test

import (
	"fmt"
	"maps"
	"math/rand/v2"
	"regexp"
	"strings"
	"testing"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// goldenKey is the key of goldenInputs, computed by a separate implementation of the encoding
// documented on NewJobKey (a Python script, not this package). If this test fails the key's encoding changed, which
// retires every cached result in every user's state directory. Change it only on purpose, and
// raise core.SchemaVersion in the same change so the retirement is named.
const goldenKey = "ad1a15d70ca0a42efa8cbc487a8571a0b6aeea68fb96b2d6526f54482d938285"

func goldenInputs() core.KeyInputs {
	return core.KeyInputs{
		Kind:        core.JobMeasure,
		Commit:      "0123456789abcdef0123456789abcdef01234567",
		TaskVersion: "kv-store@3",
		Environment: core.Fingerprint{
			Kind:        core.EnvironmentDocker,
			Backend:     core.BackendCPU,
			ImageDigest: "sha256:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
			Facts:       map[string]string{"arch": "amd64", "go": "1.25", "os": "linux"},
		},
		Settings:      core.MeasureSettings{Samples: 5},
		SchemaVersion: 1,
	}
}

func TestKeyIsPinnedByAGolden(t *testing.T) {
	key, err := core.NewJobKey(goldenInputs())
	if err != nil {
		t.Fatal(err)
	}
	if string(key) != goldenKey {
		t.Fatalf("key %s, want %s: the key encoding changed, see the comment on goldenKey", key, goldenKey)
	}
}

var hexKey = regexp.MustCompile(`^[0-9a-f]{64}$`)

// randomKeyInputs draws valid inputs for a check or measure job.
func randomKeyInputs(r *rand.Rand) core.KeyInputs {
	in := core.KeyInputs{
		Kind:          []core.JobKind{core.JobCheck, core.JobMeasure}[r.IntN(2)],
		Commit:        commitN(r.IntN(1 << 30)),
		TaskVersion:   fmt.Sprintf("task@%d", r.IntN(50)),
		Environment:   randomFingerprint(r),
		SchemaVersion: uint32(1 + r.IntN(5)),
	}
	if in.Kind == core.JobCheck {
		in.Settings = core.CheckSettings{}
	} else {
		in.Settings = core.MeasureSettings{Samples: uint32(r.IntN(20)), Recheck: r.IntN(2) == 0}
	}
	return in
}

// pieces are what names and values are made of. They include the characters an encoding would use
// to separate fields, and the empty string, so a boundary that is not length-prefixed collides.
var pieces = []string{"", "a", "ab", "b", "=", ",", "\x00", "\n", "linux", "amd64", ":"}

func randomFingerprint(r *rand.Rand) core.Fingerprint {
	f := core.Fingerprint{Backend: []core.BackendKind{core.BackendCPU, core.BackendCUDA, core.BackendMetal}[r.IntN(3)]}
	if r.IntN(2) == 0 {
		f.Kind = core.EnvironmentHost
	} else {
		f.Kind = core.EnvironmentDocker
		f.ImageDigest = fmt.Sprintf("sha256:%064x", r.Uint64())
	}
	for range r.IntN(4) {
		if f.Facts == nil {
			f.Facts = map[string]string{}
		}
		f.Facts[pieces[r.IntN(len(pieces))]+pieces[r.IntN(len(pieces))]] = pieces[r.IntN(len(pieces))]
	}
	return f
}

func keyOf(t testing.TB, in core.KeyInputs) core.JobKey {
	t.Helper()
	key, err := core.NewJobKey(in)
	if err != nil {
		t.Fatalf("NewJobKey(%+v): %v", in, err)
	}
	return key
}

func TestKeyIsStableAndCanonical(t *testing.T) {
	r := newRand(10)
	for range properties {
		in := randomKeyInputs(r)
		key := keyOf(t, in)
		if !hexKey.MatchString(string(key)) {
			t.Fatalf("key %q is not 64 lowercase hex digits", key)
		}
		if again := keyOf(t, in); again != key {
			t.Fatalf("the same inputs gave %s and %s", key, again)
		}
		// A copy built another way, with the same facts inserted in the reverse order and a nil
		// map where there are none, is the same key.
		copied := in
		copied.Environment.Facts = nil
		if len(in.Environment.Facts) > 0 {
			copied.Environment.Facts = map[string]string{}
			names := make([]string, 0, len(in.Environment.Facts))
			for name := range in.Environment.Facts {
				names = append(names, name)
			}
			for i := len(names) - 1; i >= 0; i-- {
				copied.Environment.Facts[names[i]] = in.Environment.Facts[names[i]]
			}
		}
		if got := keyOf(t, copied); got != key {
			t.Fatalf("an equal fingerprint built another way gave %s, want %s", got, key)
		}
		if empty := keyWithFacts(t, in, map[string]string{}); empty != keyWithFacts(t, in, nil) {
			t.Fatalf("a nil and an empty set of facts are two keys")
		}
	}
}

func keyWithFacts(t testing.TB, in core.KeyInputs, facts map[string]string) core.JobKey {
	in.Environment.Facts = facts
	return keyOf(t, in)
}

// Every input the contract names is in the key: changing any one changes it.
func TestEveryKeyInputChangesTheKey(t *testing.T) {
	r := newRand(11)
	changes := map[string]func(in core.KeyInputs) core.KeyInputs{
		"commit": func(in core.KeyInputs) core.KeyInputs {
			in.Commit = commitN(int(in.Commit[len(in.Commit)-1]) + 12345678)
			return in
		},
		"task version": func(in core.KeyInputs) core.KeyInputs { in.TaskVersion += "x"; return in },
		"schema version": func(in core.KeyInputs) core.KeyInputs {
			in.SchemaVersion++
			return in
		},
		"environment kind": func(in core.KeyInputs) core.KeyInputs {
			if in.Environment.Kind == core.EnvironmentHost {
				in.Environment.Kind, in.Environment.ImageDigest = core.EnvironmentDocker, fmt.Sprintf("sha256:%064x", 7)
			} else {
				in.Environment.Kind, in.Environment.ImageDigest = core.EnvironmentHost, ""
			}
			return in
		},
		"backend": func(in core.KeyInputs) core.KeyInputs {
			if in.Environment.Backend == core.BackendCPU {
				in.Environment.Backend = core.BackendROCm
			} else {
				in.Environment.Backend = core.BackendCPU
			}
			return in
		},
		"image digest": func(in core.KeyInputs) core.KeyInputs {
			in.Environment.ImageDigest = fmt.Sprintf("sha256:%064x", 99)
			return in
		},
		"a fact's value": func(in core.KeyInputs) core.KeyInputs {
			in.Environment.Facts = withFact(in.Environment.Facts, "toolchain", "a")
			return in
		},
		"a fact's name": func(in core.KeyInputs) core.KeyInputs {
			in.Environment.Facts = withFact(in.Environment.Facts, "other-toolchain", "a")
			return in
		},
		"the job kind": func(in core.KeyInputs) core.KeyInputs {
			if in.Kind == core.JobCheck {
				in.Kind, in.Settings = core.JobMeasure, core.MeasureSettings{}
			} else {
				in.Kind, in.Settings = core.JobCheck, core.CheckSettings{}
			}
			return in
		},
	}
	for range properties / 4 {
		in := randomKeyInputs(r)
		base := keyOf(t, in)
		for name, change := range changes {
			if name == "image digest" && in.Environment.Kind != core.EnvironmentDocker {
				continue // a host environment has no image digest to change
			}
			changed := in
			changed.Environment.Facts = maps.Clone(in.Environment.Facts)
			if got := keyOf(t, change(changed)); got == base {
				t.Fatalf("changing the %s did not change the key of %+v", name, in)
			}
		}
	}
}

func withFact(facts map[string]string, name, value string) map[string]string {
	out := map[string]string{}
	for k, v := range facts {
		out[k] = v
	}
	out[name] = value
	return out
}

// What changes how a job runs, and not what it measures, is not in the key: a measure job's
// sample count is topped up from the cache, and a recheck chooses where its check pass comes from.
func TestMeasureSettingsAreNotInTheKey(t *testing.T) {
	r := newRand(12)
	for range properties {
		in := randomKeyInputs(r)
		in.Kind = core.JobMeasure
		in.Settings = core.MeasureSettings{}
		want := keyOf(t, in)
		in.Settings = core.MeasureSettings{Samples: uint32(r.IntN(1000)), Recheck: r.IntN(2) == 0}
		if got := keyOf(t, in); got != want {
			t.Fatalf("settings %+v changed the key", in.Settings)
		}
	}
}

// Moving text from one field to the next must not collide: the encoding is length prefixed.
func TestFieldsCannotSwapText(t *testing.T) {
	seen := map[core.JobKey]core.KeyInputs{}
	r := newRand(13)
	for range properties * 20 {
		in := randomKeyInputs(r)
		in.Environment = core.Fingerprint{Kind: core.EnvironmentHost, Backend: core.BackendCPU, Facts: map[string]string{}}
		for range 1 + r.IntN(3) {
			in.Environment.Facts[pieces[r.IntN(len(pieces))]] = pieces[r.IntN(len(pieces))]
		}
		in.TaskVersion = pieces[1+r.IntN(len(pieces)-1)] + pieces[r.IntN(len(pieces))]
		key := keyOf(t, in)
		if other, collided := seen[key]; collided && !sameInputs(other, in) {
			t.Fatalf("%+v and %+v share the key %s", other, in, key)
		}
		seen[key] = in
	}
}

func sameInputs(a, b core.KeyInputs) bool {
	return a.Kind == b.Kind && a.Commit == b.Commit && a.TaskVersion == b.TaskVersion && a.SchemaVersion == b.SchemaVersion &&
		a.Environment.Equal(b.Environment)
}

func TestKeyRefusesInputsThatCannotHaveOne(t *testing.T) {
	valid := goldenInputs()
	cases := []struct {
		name   string
		change func(in *core.KeyInputs)
		names  string
	}{
		{"command job", func(in *core.KeyInputs) { in.Kind, in.Settings = core.JobCommand, core.CommandSettings{} }, "command"},
		{"unknown kind", func(in *core.KeyInputs) { in.Kind = "build" }, "kind"},
		{"no settings", func(in *core.KeyInputs) { in.Settings = nil }, "settings"},
		{"settings of another kind", func(in *core.KeyInputs) { in.Settings = core.CheckSettings{} }, "settings"},
		{"empty commit", func(in *core.KeyInputs) { in.Commit = "" }, "commit"},
		{"short commit", func(in *core.KeyInputs) { in.Commit = "abc123" }, "commit"},
		{"upper case commit", func(in *core.KeyInputs) { in.Commit = core.Commit(strings.ToUpper(string(in.Commit))) }, "commit"},
		{"commit that is not hex", func(in *core.KeyInputs) { in.Commit = core.Commit(strings.Repeat("g", 40)) }, "commit"},
		{"branch name", func(in *core.KeyInputs) { in.Commit = "main" }, "commit"},
		{"no task version", func(in *core.KeyInputs) { in.TaskVersion = "" }, "task version"},
		{"no schema version", func(in *core.KeyInputs) { in.SchemaVersion = 0 }, "schema version"},
		{"unpinned environment", func(in *core.KeyInputs) { in.Environment.Kind = core.EnvironmentAuto }, "environment"},
		{"unknown backend", func(in *core.KeyInputs) { in.Environment.Backend = "tpu" }, "backend"},
		{"docker without a digest", func(in *core.KeyInputs) { in.Environment.ImageDigest = "" }, "digest"},
		{"docker by tag", func(in *core.KeyInputs) { in.Environment.ImageDigest = "ubuntu:latest" }, "digest"},
		{"docker with a short digest", func(in *core.KeyInputs) { in.Environment.ImageDigest = "sha256:abc" }, "digest"},
		{"host with a digest", func(in *core.KeyInputs) {
			in.Environment.Kind = core.EnvironmentHost
		}, "digest"},
	}
	for _, c := range cases {
		t.Run(c.name, func(t *testing.T) {
			in := valid
			c.change(&in)
			key, err := core.NewJobKey(in)
			if failureKind(err) != core.FailureInvalidInput || !strings.Contains(err.Error(), c.names) || key != "" {
				t.Fatalf("got key %q and error %v, want an invalid_input failure naming %q", key, err, c.names)
			}
		})
	}
}

func TestCommitValidIsFortyLowercaseHexCharacters(t *testing.T) {
	r := newRand(14)
	const alphabet = "0123456789abcdefABCDEFxyz -"
	for range properties * 5 {
		n := r.IntN(45)
		b := make([]byte, n)
		for i := range b {
			b[i] = alphabet[r.IntN(len(alphabet))]
		}
		want := n == 40 && !strings.ContainsAny(string(b), "ABCDEFxyz -")
		if got := core.Commit(b).Valid(); got != want {
			t.Fatalf("Commit(%q).Valid() = %v, want %v", b, got, want)
		}
	}
	if !commitN(5).Valid() {
		t.Error("a commit made of 40 hex digits is invalid")
	}
}
