package doctor

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"
)

// Filesystem describes the file system that holds a path.
type Filesystem struct {
	// Type is the file system type ("ext4", "nfs4"); "" when it could not be determined.
	Type string
	// Device identifies the file system; 0 when unknown. Two paths with different nonzero
	// devices are on different file systems.
	Device uint64
}

// ActionKind is one kind of repair a Host can perform.
type ActionKind string

// The repairs. Each is safe to repeat and touches only what the repository can recreate or a
// setting this repository owns.
const (
	// ActionRun runs Argv in Dir.
	ActionRun ActionKind = "run"
	// ActionRemoveTree deletes the directory Path (a dependency tree an install recreates).
	ActionRemoveTree ActionKind = "remove-tree"
	// ActionMakeDirectory creates the directory Path and its parents.
	ActionMakeDirectory ActionKind = "make-directory"
	// ActionSetGitConfig sets the repository's git configuration Key to Value.
	ActionSetGitConfig ActionKind = "set-git-config"
)

// Action is one repair.
type Action struct {
	Kind  ActionKind
	Argv  []string
	Dir   string
	Path  string
	Key   string
	Value string
}

// Describe returns the action as one line a reader can run by hand.
func (a Action) Describe() string {
	switch a.Kind {
	case ActionRun:
		words := make([]string, len(a.Argv))
		for i, w := range a.Argv {
			words[i] = shellWord(w)
		}
		return fmt.Sprintf("(cd %s && %s)", shellWord(a.Dir), strings.Join(words, " "))
	case ActionRemoveTree:
		return "rm -rf " + shellWord(a.Path)
	case ActionMakeDirectory:
		return "mkdir -p " + shellWord(a.Path)
	case ActionSetGitConfig:
		return fmt.Sprintf("git config %s %s", a.Key, shellWord(a.Value))
	}
	return string(a.Kind)
}

var plainWord = regexp.MustCompile(`^[A-Za-z0-9_@%+=:,./-]+$`)

func shellWord(s string) string {
	if plainWord.MatchString(s) {
		return s
	}
	return Quote(s)
}

// Host is everything the doctor touches on the machine. Paths are absolute.
type Host interface {
	Getenv(name string) string
	// Exists reports whether a file or directory is at path.
	Exists(path string) bool
	ReadFile(path string) ([]byte, error)
	// Run runs argv in dir and returns its combined output, ending it after a short bound.
	Run(dir string, argv ...string) (string, error)
	// Filesystem describes the file system that would hold path, or its nearest existing parent.
	Filesystem(path string) (Filesystem, error)
	// GitConfig returns the repository's git configuration value, "" when unset.
	GitConfig(key string) string
	// LinkedWorktree reports whether the repository root is a linked worktree.
	LinkedWorktree() bool
	// PlanChanges reports why the gate cannot plan the working tree's changes, nil when it can.
	PlanChanges() error
	// Execute performs a repair.
	Execute(Action) error
}

// Copy is one executable found for a program.
type Copy struct {
	// Directory is the absolute directory holding it.
	Directory string
	// Version is what the copy reports; "" when not asked for or not parseable.
	Version string
}

// ProgramFacts is what was found for one configured program.
type ProgramFacts struct {
	Name string
	// Required is the version the program must have, "" for presence only.
	Required string
	// RequiredError says why the repository's pin could not be read.
	RequiredError string
	// OnPath lists the copies in PATH order; InRepo the copies in the repository directories.
	OnPath []Copy
	InRepo []Copy
}

// InstallFacts is what was found for one configured install.
type InstallFacts struct {
	DirectoryExists bool
	MarkerExists    bool
}

// Candidate is a directory offered as TMPDIR.
type Candidate struct {
	Path       string
	Filesystem Filesystem
}

// TempFacts is what was found about the temporary directory.
type TempFacts struct {
	// Dir is the TMPDIR in force, "/tmp" when unset.
	Dir        string
	Filesystem Filesystem
	Root       Filesystem
	// Candidate is the best local directory to use instead, nil when there is none.
	Candidate *Candidate
}

// Reference is a repository path an instruction file names.
type Reference struct {
	File   string
	Path   string
	Exists bool
}

// Facts is everything Evaluate decides from.
type Facts struct {
	Root           string
	Env            map[string]string
	LinkedWorktree bool
	HooksPath      string
	PlanError      string
	Programs       []ProgramFacts
	Installs       []InstallFacts
	Temp           TempFacts
	References     []Reference
}

// Gather reads the machine into Facts. A probe that fails is recorded as a fact (an unreadable
// pin, an unknown file system) and never stops the others.
func Gather(cfg Config, root string, host Host) Facts {
	f := Facts{Root: root, Env: map[string]string{}, LinkedWorktree: host.LinkedWorktree(), HooksPath: host.GitConfig("core.hooksPath")}
	names := append([]string{"PATH", "TMPDIR"}, cfg.Temp.candidateVariables()...)
	for _, flag := range cfg.EnvFlags {
		names = append(names, flag.Name)
	}
	for _, n := range names {
		f.Env[n] = host.Getenv(n)
	}
	if err := host.PlanChanges(); err != nil {
		f.PlanError = err.Error()
	}
	for _, p := range cfg.Programs {
		f.Programs = append(f.Programs, gatherProgram(p, root, f.Env["PATH"], host))
	}
	for _, in := range cfg.Installs {
		f.Installs = append(f.Installs, InstallFacts{
			DirectoryExists: host.Exists(filepath.Join(root, in.Directory)),
			MarkerExists:    host.Exists(filepath.Join(root, in.Marker)),
		})
	}
	f.Temp = gatherTemp(cfg.Temp, root, f.Env, host)
	f.References = gatherReferences(cfg.Instructions, root, host)
	return f
}

// maxCopiesProbed bounds how many copies of one program are asked for a version.
const maxCopiesProbed = 8

func gatherProgram(p ProgramConfig, root, path string, host Host) ProgramFacts {
	pf := ProgramFacts{Name: p.Name}
	pin := p.MinimumFrom
	if pin == nil {
		pin = p.ExactFrom
	}
	if pin != nil {
		v, err := ReadPin(host, root, *pin)
		pf.Required, pf.RequiredError = v, ""
		if err != nil {
			pf.RequiredError = err.Error()
		}
	}
	probe := pf.Required != ""
	find := func(dirs []string) []Copy {
		var copies []Copy
		seen := map[string]bool{}
		for _, dir := range dirs {
			if dir == "" || !filepath.IsAbs(dir) || seen[dir] || !host.Exists(filepath.Join(dir, p.Name)) {
				continue
			}
			seen[dir] = true
			c := Copy{Directory: dir}
			if probe && len(copies) < maxCopiesProbed {
				out, _ := host.Run(root, filepath.Join(dir, p.Name), "--version")
				c.Version = ParseVersion(out)
			}
			copies = append(copies, c)
		}
		return copies
	}
	pf.OnPath = find(filepath.SplitList(path))
	repoDirs := make([]string, len(p.RepoDirectories))
	for i, d := range p.RepoDirectories {
		repoDirs[i] = filepath.Join(root, d)
	}
	pf.InRepo = find(repoDirs)
	return pf
}

// ReadPin reads the version a PinConfig names from its file.
func ReadPin(host Host, root string, pin PinConfig) (string, error) {
	data, err := host.ReadFile(filepath.Join(root, pin.File))
	if err != nil {
		return "", fmt.Errorf("cannot read the version pin in %s: %v", pin.File, err)
	}
	m := regexp.MustCompile(pin.Pattern).FindSubmatch(data)
	if m == nil {
		return "", fmt.Errorf("%s has no line matching /%s/", pin.File, pin.Pattern)
	}
	return string(m[1]), nil
}

func gatherTemp(cfg TempConfig, root string, env map[string]string, host Host) TempFacts {
	t := TempFacts{Dir: env["TMPDIR"]}
	if t.Dir == "" {
		t.Dir = "/tmp"
	}
	t.Filesystem, _ = host.Filesystem(t.Dir)
	t.Root, _ = host.Filesystem(root)
	for _, template := range cfg.Candidates {
		p, ok := expandCandidate(template, env)
		if !ok {
			continue
		}
		fs, err := host.Filesystem(p)
		if err != nil || fs.Type == "" || IsNetworkFilesystem(fs.Type) {
			continue
		}
		c := &Candidate{Path: p, Filesystem: fs}
		if t.Candidate == nil || (!sameDevice(t.Candidate.Filesystem, t.Root) && sameDevice(fs, t.Root)) {
			t.Candidate = c
		}
	}
	return t
}

func sameDevice(a, b Filesystem) bool { return a.Device != 0 && a.Device == b.Device }

func gatherReferences(cfg InstructionsConfig, root string, host Host) []Reference {
	var refs []Reference
	for _, file := range cfg.Files {
		data, err := host.ReadFile(filepath.Join(root, file))
		if err != nil {
			continue
		}
		for _, p := range ExtractReferences(string(data), cfg.Roots) {
			refs = append(refs, Reference{File: file, Path: p, Exists: host.Exists(filepath.Join(root, p))})
		}
	}
	return refs
}

var referenceToken = regexp.MustCompile("[A-Za-z0-9_./*{}-]+")

// ExtractReferences returns the distinct repository paths the text names under one of the roots
// ("scripts/", ".githooks/"), in order of first appearance. A leading "./" is dropped, and a
// token ending in "/" or holding a glob or placeholder character names no single path and is
// left out.
func ExtractReferences(text string, roots []string) []string {
	var out []string
	seen := map[string]bool{}
	for _, tok := range referenceToken.FindAllString(text, -1) {
		tok = strings.TrimPrefix(tok, "./")
		tok = strings.TrimRight(tok, ".")
		if strings.ContainsAny(tok, "*{}") || strings.HasSuffix(tok, "/") || seen[tok] {
			continue
		}
		for _, root := range roots {
			if strings.HasPrefix(tok, root) && len(tok) > len(root) {
				seen[tok] = true
				out = append(out, tok)
				break
			}
		}
	}
	return out
}

var variableReference = regexp.MustCompile(`\$([A-Za-z_][A-Za-z0-9_]*)`)

// expandCandidate replaces each $NAME in a candidate directory with its value. It reports false
// when a variable is unset or empty, or the result is not an absolute path.
func expandCandidate(template string, env map[string]string) (string, bool) {
	ok := true
	out := variableReference.ReplaceAllStringFunc(template, func(ref string) string {
		v := env[ref[1:]]
		if v == "" {
			ok = false
		}
		return v
	})
	return filepath.Clean(out), ok && filepath.IsAbs(out)
}
