package devlaunch

import (
	"errors"
	"math/rand"
	"reflect"
	"strings"
	"testing"
	"testing/quick"
)

func kinds(steps []Step) []StepKind {
	out := make([]StepKind, len(steps))
	for i, s := range steps {
		out[i] = s.Kind
	}
	return out
}

func current() InstallState {
	return InstallState{Fingerprint: "f1", Stamp: "f1", ModulesPresent: true}
}

func TestLocalPlanTable(t *testing.T) {
	for _, tc := range []struct {
		name    string
		install InstallState
		want    []StepKind
	}{
		{"up to date skips install", current(), []StepKind{StepBuild, StepPython, StepApp}},
		{"changed manifests", InstallState{"f2", "f1", true}, []StepKind{StepInstall, StepBuild, StepPython, StepApp}},
		{"never installed", InstallState{"f1", "", true}, []StepKind{StepInstall, StepBuild, StepPython, StepApp}},
		{"modules deleted despite stamp", InstallState{"f1", "f1", false}, []StepKind{StepInstall, StepBuild, StepPython, StepApp}},
	} {
		t.Run(tc.name, func(t *testing.T) {
			steps, err := PlanSteps(Inputs{Install: tc.install})
			if err != nil || !reflect.DeepEqual(kinds(steps), tc.want) {
				t.Fatalf("got %v, %v; want %v", kinds(steps), err, tc.want)
			}
		})
	}
}

func TestMachinePlanTable(t *testing.T) {
	clean := LocalGit{Head: "aaa"}
	tail := []StepKind{StepSync, StepHelper, StepApp}
	for _, tc := range []struct {
		name    string
		git     LocalGit
		remote  RemoteState
		want    []StepKind
		refusal string
	}{
		{"remote at another commit", clean, RemoteState{Kind: CheckoutGit, Checkout: "/r", Head: "bbb"}, append([]StepKind{StepBuild, StepPython, StepShipGit}, tail...), ""},
		{"remote already at head ships nothing", clean, RemoteState{Kind: CheckoutGit, Checkout: "/r", Head: "aaa"}, append([]StepKind{StepBuild, StepPython}, tail...), ""},
		{"plain directory is copied", clean, RemoteState{Kind: CheckoutPlain, Checkout: "/r"}, append([]StepKind{StepBuild, StepPython, StepShipFiles}, tail...), ""},
		{"local changes refuse", LocalGit{Head: "aaa", Dirty: true}, RemoteState{Kind: CheckoutGit, Checkout: "/r", Head: "bbb"}, nil, "local tree"},
		{"remote changes refuse", clean, RemoteState{Kind: CheckoutGit, Checkout: "/r", Head: "bbb", Dirty: true}, nil, "/r"},
		{"no checkout refuses with what was tried", clean, RemoteState{Kind: CheckoutMissing, Tried: []string{"~/a", "~/b"}}, nil, "~/a, ~/b"},
	} {
		t.Run(tc.name, func(t *testing.T) {
			steps, err := PlanSteps(Inputs{Install: current(), Git: tc.git, Remote: &tc.remote})
			if tc.refusal != "" {
				var r Refusal
				if !errors.As(err, &r) || !strings.Contains(r.Reason, tc.refusal) || steps != nil {
					t.Fatalf("got %v, %v; want a refusal mentioning %q", steps, err, tc.refusal)
				}
				return
			}
			if err != nil || !reflect.DeepEqual(kinds(steps), tc.want) {
				t.Fatalf("got %v, %v; want %v", kinds(steps), err, tc.want)
			}
		})
	}
}

// Whatever the facts, a plan is a refusal or a sequence that builds before it opens the app, opens
// it last, ships before it starts the helper, and never ships to a dirty side.
func TestPlanInvariants(t *testing.T) {
	check := func(seed int64) bool {
		rng := rand.New(rand.NewSource(seed))
		in := Inputs{
			Install: InstallState{Fingerprint: pick(rng, "a", "b"), Stamp: pick(rng, "", "a", "b"), ModulesPresent: rng.Intn(2) == 0},
			Git:     LocalGit{Head: pick(rng, "h1", "h2"), Dirty: rng.Intn(2) == 0},
		}
		if rng.Intn(3) > 0 {
			in.Remote = &RemoteState{
				Kind: []CheckoutKind{CheckoutGit, CheckoutPlain, CheckoutMissing}[rng.Intn(3)], Checkout: "/r",
				Head: pick(rng, "h1", "h2"), Dirty: rng.Intn(2) == 0,
			}
		}
		steps, err := PlanSteps(in)
		if err != nil {
			var r Refusal
			dirtyLocal := in.Remote != nil && in.Git.Dirty
			missing := in.Remote != nil && in.Remote.Kind == CheckoutMissing
			dirtyRemote := in.Remote != nil && in.Remote.Kind == CheckoutGit && in.Remote.Dirty
			return errors.As(err, &r) && steps == nil && (dirtyLocal || missing || dirtyRemote)
		}
		ks := kinds(steps)
		pos := map[StepKind]int{}
		for i, k := range ks {
			pos[k] = i + 1
		}
		last := ks[len(ks)-1] == StepApp
		buildFirst := pos[StepBuild] > 0 && pos[StepBuild] < pos[StepApp] && (pos[StepInstall] == 0 || pos[StepInstall] < pos[StepBuild])
		remoteOrder := in.Remote == nil || (pos[StepSync] > 0 && pos[StepHelper] > pos[StepSync] &&
			(pos[StepShipGit] == 0 || pos[StepShipGit] < pos[StepSync]) && (pos[StepShipFiles] == 0 || pos[StepShipFiles] < pos[StepSync]))
		localOnly := in.Remote != nil || (pos[StepSync] == 0 && pos[StepHelper] == 0 && pos[StepShipGit] == 0 && pos[StepShipFiles] == 0)
		return last && buildFirst && remoteOrder && localOnly
	}
	if err := quick.Check(check, &quick.Config{MaxCount: 500}); err != nil {
		t.Fatal(err)
	}
}

func pick(rng *rand.Rand, options ...string) string { return options[rng.Intn(len(options))] }

func TestFingerprintIgnoresOrderAndSeesChanges(t *testing.T) {
	check := func(contents []string, shuffle int64, flip uint8) bool {
		if len(contents) == 0 {
			return true
		}
		files := make([]File, len(contents))
		for i, c := range contents {
			files[i] = File{Path: "p/" + string(rune('a'+i%26)) + string(rune('a'+i/26)), Content: []byte(c)}
		}
		base := Fingerprint(files)
		shuffled := append([]File(nil), files...)
		rand.New(rand.NewSource(shuffle)).Shuffle(len(shuffled), func(i, j int) { shuffled[i], shuffled[j] = shuffled[j], shuffled[i] })
		if Fingerprint(shuffled) != base {
			return false
		}
		changed := append([]File(nil), files...)
		k := int(flip) % len(changed)
		changed[k] = File{changed[k].Path, append(append([]byte(nil), changed[k].Content...), 'x')}
		renamed := append([]File(nil), files...)
		renamed[k] = File{renamed[k].Path + "2", renamed[k].Content}
		return Fingerprint(changed) != base && Fingerprint(renamed) != base
	}
	if err := quick.Check(check, nil); err != nil {
		t.Fatal(err)
	}
}
