package task_test

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/BurntSushi/toml"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/task"
)

func decodeCatalogue(manifest string) (*cliv1.TaskCatalogueV02, error) {
	var table map[string]any
	if _, err := toml.Decode(manifest, &table); err != nil {
		return nil, err
	}
	return task.Catalogue(table["catalogue"].(map[string]any))
}

// Every [catalogue] table of the repository's examples decodes into the typed message and meets its
// rules, whichever schema version the rest of the manifest is written in.
func TestEveryExampleCatalogueDecodes(t *testing.T) {
	root, err := filepath.Abs("../../../examples")
	if err != nil {
		t.Fatal(err)
	}
	if _, err := os.Stat(root); err != nil {
		t.Skip("the examples are not in this checkout")
	}
	var seen int
	err = filepath.WalkDir(root, func(p string, d fs.DirEntry, err error) error {
		if err != nil {
			return nil
		}
		if d.IsDir() && (d.Name() == "node_modules" || d.Name() == "repositories") {
			return filepath.SkipDir
		}
		if d.IsDir() || d.Name() != task.ManifestName {
			return nil
		}
		data, err := os.ReadFile(p)
		if err != nil {
			t.Error(err)
			return nil
		}
		if !strings.Contains(string(data), "[catalogue]") {
			return nil
		}
		seen++
		if _, err := decodeCatalogue(string(data)); err != nil {
			t.Errorf("%s: %v", p, err)
		}
		return nil
	})
	if err != nil {
		t.Fatal(err)
	}
	if seen == 0 {
		t.Fatal("no example has a [catalogue]")
	}
}

func TestACatalogueIsHeldToItsDomain(t *testing.T) {
	const hardware = "[catalogue.hardware]\naccelerator = \"CPU\"\ncount = 1\nvendor = \"cpu\"\n"
	for name, c := range map[string]struct{ manifest, want string }{
		"serving needs a model":          {"[catalogue]\ndomain = \"llm-serving\"\n" + hardware, "model is required"},
		"a model belongs to serving":     {"[catalogue]\ndomain = \"databases\"\n[catalogue.model]\nname = \"m\"\nmaker = \"meta\"\n" + hardware, "model is required"},
		"structures need a workload":     {"[catalogue]\ndomain = \"data-structures\"\n" + hardware, "workload is required"},
		"an unknown key is named":        {"[catalogue]\ndomain = \"databases\"\nvariant = \"x\"\n" + hardware, "variant"},
		"an unknown domain is refused":   {"[catalogue]\ndomain = \"kernels\"\n" + hardware, "kernels"},
		"a hardware count of zero fails": {"[catalogue]\ndomain = \"databases\"\n[catalogue.hardware]\naccelerator = \"CPU\"\ncount = 0\nvendor = \"cpu\"\n", "count"},
	} {
		t.Run(name, func(t *testing.T) {
			_, err := decodeCatalogue(c.manifest)
			if err == nil || !strings.Contains(err.Error(), c.want) {
				t.Fatalf("got %v, want an error naming %q", err, c.want)
			}
		})
	}
}

// A task names its domain as the enum value's name without its prefix, in lower case, with a hyphen
// for each underscore. Every domain the contract defines is catalogued by that spelling, with the
// facts its domain requires and no others; cli-tools and kernel-writing are the two that once had no
// value, so a task of either could not be catalogued.
func TestEveryDomainIsCataloguedByItsSpelling(t *testing.T) {
	const hardware = "[catalogue.hardware]\naccelerator = \"CPU\"\ncount = 1\nvendor = \"cpu\"\n"
	facts := map[cliv1.TaskDomain]string{
		cliv1.TaskDomain_TASK_DOMAIN_LLM_SERVING:     "[catalogue.model]\nname = \"m\"\nmaker = \"meta\"\n",
		cliv1.TaskDomain_TASK_DOMAIN_DATA_STRUCTURES: "[catalogue.workload]\nname = \"w\"\ndescription = \"d\"\n",
	}
	spellings := map[string]bool{}
	values := cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED.Descriptor().Values()
	for i := range values.Len() {
		value := cliv1.TaskDomain(values.Get(i).Number())
		if value == cliv1.TaskDomain_TASK_DOMAIN_UNSPECIFIED {
			continue
		}
		spelling := strings.ReplaceAll(strings.ToLower(strings.TrimPrefix(value.String(), "TASK_DOMAIN_")), "_", "-")
		spellings[spelling] = true
		t.Run(spelling, func(t *testing.T) {
			got, err := decodeCatalogue("[catalogue]\ndomain = \"" + spelling + "\"\n" + hardware + facts[value])
			if err != nil {
				t.Fatal(err)
			}
			if got.GetDomain() != value {
				t.Fatalf("domain %q decoded as %v, want %v", spelling, got.GetDomain(), value)
			}
		})
	}
	for _, reported := range []string{"cli-tools", "kernel-writing"} {
		if !spellings[reported] {
			t.Errorf("no domain is spelled %q", reported)
		}
	}
}
