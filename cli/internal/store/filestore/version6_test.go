package filestore_test

import (
	"fmt"
	"reflect"
	"regexp"
	"slices"
	"strconv"
	"strings"
	"testing"

	"google.golang.org/protobuf/encoding/protojson"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore"
	"github.com/uw-syfi/vibesys/cli/internal/store/filestore/fsys"
	"github.com/uw-syfi/vibesys/cli/internal/store/storetest"
)

// dirNames are the directories a store holds in its root, sorted.
func dirNames(t *testing.T, mem *fsys.MemFS) []string {
	t.Helper()
	entries, err := mem.ReadDir(stateDir)
	if err != nil {
		t.Fatalf("ReadDir(%s): %v", stateDir, err)
	}
	var names []string
	for _, e := range entries {
		if e.IsDir {
			names = append(names, e.Name)
		}
	}
	slices.Sort(names)
	return names
}

var formatNumber = regexp.MustCompile(`"format":(\d+)`)

// formatOf is the version a store's format file says.
func formatOf(t *testing.T, mem *fsys.MemFS) int {
	t.Helper()
	m := formatNumber.FindStringSubmatch(unspaced(file(t, mem, stateDir+"/format")))
	if m == nil {
		t.Fatalf("the format file says no version: %s", file(t, mem, stateDir+"/format"))
	}
	n, err := strconv.Atoi(m[1])
	if err != nil {
		t.Fatal(err)
	}
	return n
}

// directoriesAddedBy lists, for each format version, the directories a store of that version has
// that one of the version before did not. A directory added to a store's layout is named here under
// the version that adds it, and that version is the one a program that does not know the directory
// refuses: without it such a program opens the store and ignores what the directory holds.
var directoriesAddedBy = map[int][]string{
	2: {"jobs", "logs", "plans", "promotions", "results", "runs"},
	3: {"frontier"},
	6: {"bounds", "workstreams"},
}

// A store holds exactly the directories of its own format version: none a later version added, and
// all that an earlier one did. So a directory is never in a store whose version does not say so.
func TestAFreshStoreHasExactlyTheDirectoriesOfItsFormatVersion(t *testing.T) {
	mem := fsys.NewMemFS()
	if err := openOn(t, mem).Close(); err != nil {
		t.Fatal(err)
	}
	version := formatOf(t, mem)
	var want []string
	for v, dirs := range directoriesAddedBy {
		if v > version {
			t.Errorf("version %d adds %v, and the program writes version %d", v, dirs, version)
		}
		want = append(want, dirs...)
	}
	slices.Sort(want)
	if got := dirNames(t, mem); !slices.Equal(got, want) {
		t.Errorf("a store of format %d holds the directories %v, want %v: name a directory added to the layout in directoriesAddedBy, under the version that adds it", version, got, want)
	}
}

// versionFive is the filesystem of a store as a program of format version 5 left it: the script's
// records stamped 5, a plan that has no origin and no transitions, and none of the directories
// version 6 added. Without bounds it is a store made before declared bounds, which has no bounds
// directory either, and no bound declared.
func versionFive(t *testing.T, withBounds bool) *fsys.MemFS {
	t.Helper()
	mem := fsys.NewMemFS()
	s := openOn(t, mem)
	for _, o := range script() {
		if !withBounds && (o.name == "declare a bound" || o.name == "revise it") {
			continue
		}
		if err := o.run(t.Context(), s); err != nil {
			t.Fatalf("%s: %v", o.name, err)
		}
	}
	image := mem.CrashImage(false)
	for name, data := range image.Files() {
		if !strings.HasPrefix(string(data), "{") || strings.HasSuffix(name, ".lock") {
			continue
		}
		if strings.HasPrefix(name, stateDir+"/plans/") {
			var record cliv1.PlanFile
			if err := protojson.Unmarshal(data, &record); err != nil {
				t.Fatal(err)
			}
			record.Plan.Origin, record.Plan.Transitions, record.Plan.Workstream = nil, nil, 0
			var err error
			if data, err = (protojson.MarshalOptions{UseProtoNames: true}).Marshal(&record); err != nil {
				t.Fatal(err)
			}
		}
		image.Put(name, withVersion(data, "5"))
	}
	for name := range image.Files() {
		if strings.HasPrefix(name, stateDir+"/workstreams/") {
			if err := image.Remove(name); err != nil {
				t.Fatalf("removing %s: %v", name, err)
			}
		}
	}
	for _, dir := range []string{"workstreams", "bounds"} {
		if dir == "bounds" && withBounds {
			continue
		}
		if err := image.Remove(stateDir + "/" + dir); err != nil {
			t.Fatalf("removing %s: %v", dir, err)
		}
	}
	return image
}

// A store of version 5 opens as it was, and is a store of the current version (7) from then on, with
// every directory version 6 added: its format file says 7, and a program of version 5 or 6 would
// refuse it. A crash at any step of the migration leaves a store that the next Open migrates and that
// shows the same, and the format file says 7 only once every directory is there. A plan the older program
// saved has no origin and no transitions, and none is invented.
func TestAStoreOfVersionFiveIsMigratedToTheCurrentVersion(t *testing.T) {
	for _, withBounds := range []bool{true, false} {
		t.Run(fmt.Sprintf("with bounds %v", withBounds), func(t *testing.T) {
			ref := versionFive(t, withBounds)
			if got := formatOf(t, ref); got != 5 {
				t.Fatalf("the fixture is a store of format %d, want 5", got)
			}
			want := storetest.Observe(t, openOn(t, ref.CrashImage(false)), runs...)
			wantDirs := slices.Concat(directoriesAddedBy[2], directoriesAddedBy[3], directoriesAddedBy[6])
			slices.Sort(wantDirs)

			for step := 0; ; step++ {
				mem := ref.CrashImage(false)
				mem.SetFault(fsys.Fault{At: step, Kind: fsys.FaultCrash})
				opened, err := filestore.Open(stateDir, filestore.WithFS(mem))
				if err == nil {
					if got := formatOf(t, mem); got != 7 {
						t.Fatalf("format after the migration = %d, want 7", got)
					}
					if got := dirNames(t, mem); !slices.Equal(got, wantDirs) {
						t.Fatalf("directories after the migration = %v, want %v", got, wantDirs)
					}
					if got := storetest.Observe(t, opened, runs...); !reflect.DeepEqual(got, want) {
						t.Fatalf("a migrated store shows\n%+v\nwant\n%+v", got, want)
					}
					if step == 0 {
						t.Fatal("the migration took no step")
					}
					clean := mem.CrashImage(false) // the fault that ended the sweep is not part of what follows
					legacyPlanKeepsNoInventedHistory(t, clean, openOn(t, clean))
					return
				}
				for _, power := range []bool{false, true} {
					image := mem.CrashImage(power)
					if formatOf(t, image) == 7 && !slices.Equal(dirNames(t, image), wantDirs) {
						t.Fatalf("after a crash at step %d (power loss %v) the format says 7 and the directories are %v", step, power, dirNames(t, image))
					}
					if got := storetest.Observe(t, openOn(t, image), runs...); !reflect.DeepEqual(got, want) {
						t.Fatalf("after a crash at step %d (power loss %v) the store shows\n%+v\nwant\n%+v", step, power, got, want)
					}
				}
			}
		})
	}
}

// legacyPlanKeepsNoInventedHistory checks, on a migrated store, the plan an older program saved: it
// has no origin and no transitions, the first status change gives it its first transition and
// nothing before it, an edit that leaves the status as it is adds none, and the bounds directory
// takes a declaration whether or not the older store had one.
func legacyPlanKeepsNoInventedHistory(t *testing.T, mem *fsys.MemFS, s *filestore.Store) {
	t.Helper()
	plan, err := s.Plan(t.Context(), 1)
	if err != nil || plan.Origin != nil || len(plan.Transitions) != 0 || plan.Status != core.PlanChosen {
		t.Fatalf("the plan of the older program = %+v, %v; want it chosen with no origin and no transitions", plan, err)
	}
	origin := core.RecordOrigin{Run: "run-d", Agent: "lead", At: at(60)}
	chosen, won := core.PlanChosen, core.PlanWon
	same, err := s.EditPlan(t.Context(), 1, core.PlanEdit{Status: &chosen, Origin: origin})
	if err != nil || len(same.Transitions) != 0 {
		t.Fatalf("an edit that sets the status the plan has = %+v, %v; want no transition", same, err)
	}
	edited, err := s.EditPlan(t.Context(), 1, core.PlanEdit{Status: &won, Origin: origin})
	if err != nil || len(edited.Transitions) != 1 || edited.Transitions[0].Status != core.PlanWon || !edited.Transitions[0].Origin.Same(origin) || edited.Origin != nil {
		t.Fatalf("the first status change = %+v, %v; want one transition, to won, by the edit", edited, err)
	}
	if err := edited.CheckHistory(); err != nil {
		t.Fatal(err)
	}
	declared, err := s.DeclareBound(t.Context(), core.NewBound{
		Origin: origin, Objective: "ops", Direction: core.Maximize, Value: 1, Unit: "ops/s", Note: "bandwidth",
	})
	if err != nil {
		t.Fatalf("DeclareBound on a migrated store: %v", err)
	}
	if err := s.Close(); err != nil {
		t.Fatal(err)
	}
	for _, power := range []bool{false, true} {
		again := openOn(t, mem.CrashImage(power))
		got, err := again.Plan(t.Context(), 1)
		if err != nil || got.Status != core.PlanWon || len(got.Transitions) != 1 {
			t.Errorf("after reopening (power loss %v) the plan = %+v, %v; want what the edit made", power, got, err)
		}
		if bounds, err := again.Bounds(t.Context()); err != nil || !slices.ContainsFunc(bounds, func(b core.Bound) bool { return b.Seq == declared.Seq }) {
			t.Errorf("after reopening (power loss %v) Bounds = %+v, %v; want the one declared", power, bounds, err)
		}
		if err := again.Close(); err != nil {
			t.Fatal(err)
		}
	}
}
