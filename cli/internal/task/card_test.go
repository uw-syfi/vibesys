package task_test

import (
	"fmt"
	"io/fs"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"testing/fstest"
	"unicode/utf8"

	"buf.build/go/protovalidate"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

// exampleTasks lists the tasks of the repository's examples as (example directory, task name).
func exampleTasks(t *testing.T) [][2]string {
	t.Helper()
	root, err := filepath.Abs("../../../examples")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Skip("the examples are not in this checkout")
	}
	var found [][2]string
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && d.Name() == "node_modules" {
			return filepath.SkipDir
		}
		if d.IsDir() || d.Name() != task.ManifestName {
			return nil
		}
		taskDir := filepath.Dir(p)
		example := filepath.Dir(filepath.Dir(filepath.Dir(taskDir)))
		found = append(found, [2]string{example, filepath.Base(taskDir)})
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	return found
}

// Every task of the repository's examples has a card that meets the rules of its message, with a
// objectives (a task may have no heading in its OBJECTIVE.md), and every example that authors a [catalogue] has a domain and a hardware
// need that names a model unless it is a CPU or a whole vendor. None is skipped for failing to read.
func TestEveryExampleTaskHasAValidCard(t *testing.T) {
	tasks := exampleTasks(t)
	if len(tasks) < 49 {
		t.Fatalf("found %d example tasks, want at least 49", len(tasks))
	}
	headed := 0
	for _, e := range tasks {
		card, err := task.Card(os.DirFS(e[0]), e[1])
		if err != nil {
			t.Errorf("%s task %s: %v", e[0], e[1], err)
			continue
		}
		if err := protovalidate.Validate(card); err != nil {
			t.Errorf("%s task %s: card is invalid: %v", e[0], e[1], err)
		}
		if card.GetTask() != e[1] {
			t.Errorf("card names task %q, want %q", card.GetTask(), e[1])
		}
		if card.GetHeading() != "" {
			headed++
		}
		if len(card.GetObjectives()) == 0 {
			t.Errorf("%s task %s: no objectives", e[0], e[1])
		}
		if card.GetDomain() != cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED {
			if card.GetHardware() == nil || card.GetHardware().GetCount() == 0 {
				t.Errorf("%s task %s: a [catalogue] without a hardware need", e[0], e[1])
			}
			named := card.GetHardware().GetModel() != cliv1.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED
			cpu := card.GetHardware().GetVendor() == cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_CPU
			if !named && !cpu && !strings.HasSuffix(card.GetHardwareName(), " GPU") {
				t.Errorf("%s task %s: %q names no model", e[0], e[1], card.GetHardwareName())
			}
		}
	}
	if headed == 0 {
		t.Error("no card has a heading: the reader finds none")
	}
}

const validObjectives = "[[objective]]\nname = \"throughput\"\ndirection = \"max\"\nunit = \"tok/s\"\n\n[[objective]]\nname = \"p90_ms\"\ndirection = \"min\"\n"

func catalogueManifest(accelerator, vendor string) string {
	return fmt.Sprintf(`version = "0.2"
[catalogue]
domain = "llm-serving"
[catalogue.hardware]
accelerator = %q
count = 2
vendor = %q
[catalogue.model]
name = "Some Model"
maker = "meta"
`, accelerator, vendor)
}

func oneTask(manifest, objectives, description string) fs.FS {
	files := fstest.MapFS{"x/.vibesys/tasks/t/" + task.ManifestName: {Data: []byte(manifest)}}
	if objectives != "" {
		files["x/.vibesys/tasks/t/"+task.ObjectiveName] = &fstest.MapFile{Data: []byte(objectives)}
	}
	if description != "" {
		files["x/.vibesys/tasks/t/"+task.DescriptionName] = &fstest.MapFile{Data: []byte(description)}
	}
	sub, err := fs.Sub(files, "x")
	if err != nil {
		panic(err)
	}
	return sub
}

func TestACardSaysWhatTheTaskAuthored(t *testing.T) {
	card, err := task.Card(oneTask(catalogueManifest("AMD MI300X", "amd"), validObjectives, "<!-- c -->\n\n# Qwen on MI300X\n\ntext\n"), "t")
	if err != nil {
		t.Fatal(err)
	}
	want := &cliv1.AcceleratorNeed{Vendor: cliv1.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, Model: cliv1.AcceleratorModel_ACCELERATOR_MODEL_MI300X, Count: 2}
	if card.GetHardware().GetVendor() != want.Vendor || card.GetHardware().GetModel() != want.Model || card.GetHardware().GetCount() != want.Count {
		t.Errorf("hardware %v", card.GetHardware())
	}
	if card.GetHardwareName() != "AMD MI300X" || card.GetModelName() != "Some Model" || card.GetModelMaker() != cliv1.ModelMaker_MODEL_MAKER_META ||
		card.GetDomain() != cliv1.TaskDomain_TASK_DOMAIN_LLM_SERVING || card.GetHeading() != "Qwen on MI300X" {
		t.Errorf("card %v", card)
	}
	if o := card.GetObjectives(); len(o) != 2 || o[0].GetName() != "throughput" || o[0].GetUnit() != "tok/s" ||
		o[0].GetDirection() != cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MAX ||
		o[1].GetDirection() != cliv1.TaskObjectiveDirection_TASK_OBJECTIVE_DIRECTION_MIN || o[1].GetUnit() != "" {
		t.Errorf("objectives %v", o)
	}
}

// A task with no [catalogue], objectives or description still has a card: the task, nothing else.
func TestATaskWithoutACatalogueHasABareCard(t *testing.T) {
	card, err := task.Card(oneTask("version = \"0.2\"\n", "", ""), "t")
	if err != nil {
		t.Fatal(err)
	}
	if card.GetTask() != "t" || card.GetDomain() != cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED || card.GetHardware() != nil || card.GetHeading() != "" || len(card.GetObjectives()) != 0 {
		t.Errorf("card %v", card)
	}
}

func TestAMissingTaskIsNotFound(t *testing.T) {
	for _, name := range []string{"nope", "a/b", ".."} {
		_, err := task.Card(oneTask("", "", ""), name)
		want := core.FailureNotFound
		if name != "nope" {
			want = core.FailureInvalidInput
		}
		if got := core.AsFailure(err).Kind; err == nil || got != want {
			t.Errorf("%q: got %v (%v), want kind %v", name, err, got, want)
		}
	}
	// A directory with no manifest is not a task.
	files := fstest.MapFS{".vibesys/tasks/t/OBJECTIVE.md": {Data: []byte("# x")}}
	if _, err := task.Card(files, "t"); core.AsFailure(err).Kind != core.FailureNotFound {
		t.Errorf("no manifest: %v", err)
	}
}

// Over generated tables, a key no message has a field for is refused with the path of the key, in
// whichever table it is added, and a card is never returned beside the error.
func TestAnUnknownKeyIsRefusedByItsPath(t *testing.T) {
	r := rand.New(rand.NewPCG(1, 2))
	tables := map[string]func(key string) (manifest, objectives string){
		"catalogue": func(key string) (string, string) {
			return strings.Replace(catalogueManifest("CPU", "cpu"), "[catalogue]\n", "[catalogue]\n"+key+" = 1\n", 1), validObjectives
		},
		"catalogue.hardware": func(key string) (string, string) {
			return catalogueManifest("CPU", "cpu") + "", validObjectives
		},
		"objectives": func(key string) (string, string) {
			return catalogueManifest("CPU", "cpu"), validObjectives + "\n[[objective]]\nname = \"z\"\ndirection = \"max\"\n" + key + " = 1\n"
		},
	}
	for i := 0; i < 60; i++ {
		key := fmt.Sprintf("k%c%d", 'a'+rune(r.IntN(26)), r.IntN(1000))
		for where, build := range tables {
			manifest, objectives := build(key)
			if where == "catalogue.hardware" {
				manifest = strings.Replace(manifest, "count = 2\n", "count = 2\n"+key+" = 1\n", 1)
			}
			card, err := task.Card(oneTask(manifest, objectives, ""), "t")
			if err == nil || card != nil {
				t.Fatalf("%s: unknown key %s accepted", where, key)
			}
			if !strings.Contains(err.Error(), key) || core.AsFailure(err).Kind != core.FailureInvalidInput {
				t.Errorf("%s: error %q does not name %s as invalid input", where, err, key)
			}
			file := task.ManifestName
			if where == "objectives" {
				file = task.ObjectiveName
			}
			if !strings.Contains(err.Error(), file) {
				t.Errorf("%s: error %q does not name the file %s", where, err, file)
			}
		}
	}
}

// Every registered name makes a card with its own vendor and model, and with another vendor makes
// an error naming the key, whatever the count.
func TestTheHardwareOfACardFollowsTheRegistry(t *testing.T) {
	vendorWords := map[cliv1.AcceleratorVendor]string{}
	for v, name := range cliv1.AcceleratorVendor_name {
		if v != 0 {
			vendorWords[cliv1.AcceleratorVendor(v)] = strings.ToLower(strings.TrimPrefix(name, "ACCELERATOR_VENDOR_"))
		}
	}
	for _, name := range task.AcceleratorNames() {
		a, _ := task.LookupAccelerator(name)
		card, err := task.Card(oneTask(catalogueManifest(name, vendorWords[a.Vendor]), validObjectives, ""), "t")
		if err != nil {
			t.Errorf("%s: %v", name, err)
			continue
		}
		if h := card.GetHardware(); h.GetVendor() != a.Vendor || h.GetModel() != a.Model || card.GetHardwareName() != name {
			t.Errorf("%s: hardware %v", name, h)
		}
		for v, word := range vendorWords {
			if v == a.Vendor {
				continue
			}
			_, err := task.Card(oneTask(catalogueManifest(name, word), validObjectives, ""), "t")
			if err == nil || !strings.Contains(err.Error(), "vendor") || !strings.Contains(err.Error(), name) {
				t.Errorf("%s authored as %s: %v", name, word, err)
			}
		}
	}
}

// A name the registry does not have is an error naming the file, the table and the key, never a
// card with a device of OTHER.
func TestAnUnknownAcceleratorNamesTheFileAndKey(t *testing.T) {
	r := rand.New(rand.NewPCG(3, 4))
	for i := 0; i < 50; i++ {
		name := fmt.Sprintf("Acme %c%d", 'A'+rune(r.IntN(26)), r.IntN(10000))
		card, err := task.Card(oneTask(catalogueManifest(name, "nvidia"), validObjectives, ""), "t")
		if card != nil || err == nil {
			t.Fatalf("%q accepted", name)
		}
		for _, want := range []string{task.ManifestName, "[catalogue.hardware]", "accelerator", name} {
			if !strings.Contains(err.Error(), want) {
				t.Errorf("error %q lacks %q", err, want)
			}
		}
	}
	// The match is exact: a near spelling is not the model.
	for _, name := range []string{"amd mi300x", "AMD MI300X ", "AMD  MI300X", "MI300X", ""} {
		if _, err := task.Card(oneTask(catalogueManifest(name, "amd"), validObjectives, ""), "t"); err == nil {
			t.Errorf("%q accepted", name)
		}
	}
}

// The heading is the first heading outside a code fence, whatever surrounds it, and always fits the
// card as clean UTF-8.
func TestTheHeadingIsTheFirstOneOutsideFences(t *testing.T) {
	r := rand.New(rand.NewPCG(5, 6))
	noise := []string{"", "plain text", "```\n# in a fence\n```", "~~~sh\n## also fenced\n~~~", "    # indented code", "#nospace", "text #hash", "<!-- # c -->"}
	for i := 0; i < 200; i++ {
		var before, after []string
		for range r.IntN(4) {
			before = append(before, noise[r.IntN(len(noise))])
		}
		for range r.IntN(4) {
			after = append(after, "# later\n"+noise[r.IntN(len(noise))])
		}
		doc := strings.Join(before, "\n") + "\n#  The Title  ##\n" + strings.Join(after, "\n")
		if got := task.Heading([]byte(doc)); got != "The Title" {
			t.Fatalf("Heading(%q) = %q", doc, got)
		}
		if got := task.Heading([]byte(strings.Join(before, "\n"))); got != "" {
			t.Fatalf("Heading with no heading: %q from %q", got, before)
		}
	}
	for _, level := range []string{"#", "##", "###"} {
		if got := task.Heading([]byte(level + " Level\r\nbody")); got != "Level" {
			t.Errorf("%s: %q", level, got)
		}
	}
	for n := 0; n < 400; n += 7 {
		long := "# " + strings.Repeat("é", n)
		got := task.Heading([]byte(long))
		if len(got) > 200 || !utf8.ValidString(got) || got != strings.TrimSpace(got) {
			t.Fatalf("n=%d: bad heading of %d bytes", n, len(got))
		}
	}
}

func TestACardAlwaysMeetsItsRules(t *testing.T) {
	r := rand.New(rand.NewPCG(7, 8))
	for i := 0; i < 100; i++ {
		names := task.AcceleratorNames()
		name := names[r.IntN(len(names))]
		a, _ := task.LookupAccelerator(name)
		vendor := strings.ToLower(strings.TrimPrefix(a.Vendor.String(), "ACCELERATOR_VENDOR_"))
		long := strings.Repeat("w", r.IntN(300))
		card, err := task.Card(oneTask(catalogueManifest(name, vendor), validObjectives, "# "+long+"\n"), "t")
		if err != nil {
			t.Fatal(err)
		}
		if err := protovalidate.Validate(card); err != nil {
			t.Fatalf("%s: %v", name, err)
		}
	}
}
