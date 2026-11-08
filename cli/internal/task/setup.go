package task

import (
	"io/fs"
	"path"

	"github.com/BurntSushi/toml"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Draft is what the setup of a task reads of its files in the working tree: the card, what the
// manifest and the objectives declare, and the version of the whole directory. A draft need not be
// runnable: it has no benchmark yet, or no check, which Declarations says.
type Draft struct {
	Card         *cliv1.TaskCard
	Declarations *cliv1.SetupDeclarations
	// Version names every file of the task's directory (Version), so it moves with any edit to it.
	Version string
}

// DraftOf reads the draft task called name from a tree that holds every file of its directory
// (CardFiles.WorkingTask). It fails as Card does for a task that is not there (core.FailureNotFound)
// or whose card files are invalid (core.FailureInvalidInput, naming the file and the key), and with
// core.FailureInvalidInput for a directory whose version cannot be taken (a file that is neither a
// regular file nor a link).
func DraftOf(tree Tree, name string) (Draft, error) {
	card, err := Card(tree, name)
	if err != nil {
		return Draft{}, err
	}
	dir := path.Join(TasksDir, name)
	manifest, err := fs.ReadFile(tree, path.Join(dir, ManifestName))
	if err != nil {
		return Draft{}, &core.Failure{Kind: core.FailureNotFound, Message: "task " + quote(name) + ": no " + ManifestName + " in " + dir}
	}
	objectives, err := readOptional(tree, path.Join(dir, ObjectiveName))
	if err != nil {
		return Draft{}, err
	}
	decl, err := declarationsOf(manifest, objectives)
	if err != nil {
		f := core.AsFailure(err)
		return Draft{}, &core.Failure{Kind: f.Kind, Message: "task " + quote(name) + " (" + dir + "): " + f.Message}
	}
	version, err := Version(tree, dir)
	if err != nil {
		f := core.AsFailure(err)
		return Draft{}, &core.Failure{Kind: core.FailureInvalidInput, Message: "task " + quote(name) + " (" + dir + "): " + f.Message}
	}
	return Draft{Card: card, Declarations: decl, Version: version}, nil
}

// maxSetupObjectives is the bound of SetupDeclarations.objectives, which is also the card's.
const maxSetupObjectives = 16

// declarationsOf says what the files declare, as a draft's setup checks need it: the objectives
// with the noise floor each carries, whether the manifest declares a correctness check
// ([accuracy], or [check], with a command) and a benchmark ([benchmark], or [measure], with a
// command). It reads no more of the manifest than that, so a manifest that is not finished is
// still read.
func declarationsOf(manifestTOML, objectivesTOML []byte) (*cliv1.SetupDeclarations, error) {
	var table map[string]any
	if _, err := toml.Decode(string(manifestTOML), &table); err != nil {
		return nil, invalid("%s: %v", ManifestName, err)
	}
	o, err := ParseObjectives(objectivesTOML)
	if err != nil {
		return nil, err
	}
	out := &cliv1.SetupDeclarations{
		CheckDeclared:     hasCommand(table, "accuracy") || hasCommand(table, "check"),
		BenchmarkDeclared: hasCommand(table, "benchmark") || hasCommand(table, "measure"),
	}
	objectives := o.GetObjective()
	if len(objectives) > maxSetupObjectives {
		return nil, invalid("%s: %d objectives are declared, and the most is %d", ObjectiveName, len(objectives), maxSetupObjectives)
	}
	for _, obj := range objectives {
		s := &cliv1.SetupObjective{Name: obj.GetName(), Direction: obj.GetDirection(), Unit: obj.GetUnit()}
		switch {
		case obj.RelativeNoise != nil:
			floor := obj.GetRelativeNoise()
			s.NoiseFloor = &floor
		case o.GetPareto() != nil && len(objectives) > 1:
			// The floor of a task judged on every objective together, as the benchmark's scoring reads it.
			floor := o.GetPareto().GetRelativeNoise()
			s.NoiseFloor = &floor
		}
		out.Objectives = append(out.Objectives, s)
	}
	return out, nil
}

// hasCommand says whether the manifest has the section, as a table with a command of at least one
// element.
func hasCommand(table map[string]any, section string) bool {
	s, ok := table[section].(map[string]any)
	if !ok {
		return false
	}
	argv, ok := s["command"].([]any)
	return ok && len(argv) > 0
}
