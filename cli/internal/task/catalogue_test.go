package task_test

import (
	"io/fs"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"buf.build/go/protovalidate"
	"github.com/BurntSushi/toml"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/task"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

func decodeCatalogue(manifest string) (*cliv1.TaskCatalogueV02, error) {
	var table map[string]any
	if _, err := toml.Decode(manifest, &table); err != nil {
		return nil, err
	}
	var m cliv1.TaskManifestV02
	declared := task.Schema02
	err := tomlproto.DecodeTable(map[string]any{"version": "0.2", "catalogue": table["catalogue"]}, &m, tomlproto.Options{Declared: &declared})
	if err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(m.GetCatalogue()); err != nil {
		return nil, err
	}
	return m.GetCatalogue(), nil
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
