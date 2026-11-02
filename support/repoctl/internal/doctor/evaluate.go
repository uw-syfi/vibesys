package doctor

import (
	"fmt"
	"path/filepath"
	"strings"
)

// Status is the outcome of one check.
type Status string

// The outcomes. A Warn names a problem that does not stop the gate on its own; a Fail names one
// that does, or that makes a result untrustworthy.
const (
	Pass Status = "pass"
	Warn Status = "warn"
	Fail Status = "FAIL"
)

// Finding is the result of one check: what was found and, when it is not a pass, the exact fix.
type Finding struct {
	Check   string
	Status  Status
	Message string
	// Fix is the exact remedy as text a reader can follow, "" for a pass.
	Fix string
	// Actions are the repairs `--fix` performs, in order; empty when the fix is by hand.
	Actions []Action
	// Exports are the environment changes that complete the fix; `--env` prints them.
	Exports []Export
}

// Evaluate decides every check from the facts. It does no I/O: the same policy and facts give the
// same findings, in policy order.
func Evaluate(cfg Config, f Facts) []Finding {
	var out []Finding
	out = append(out, hooksFinding(cfg.Hooks, f))
	out = append(out, planFinding(f))
	for i, p := range cfg.Programs {
		out = append(out, programFinding(cfg, p, f.Programs[i], f))
	}
	for i, in := range cfg.Installs {
		out = append(out, installFinding(in, f.Installs[i], f.Root))
	}
	out = append(out, tempFinding(cfg.Temp, f))
	for _, flag := range cfg.EnvFlags {
		out = append(out, envFlagFinding(flag, f))
	}
	out = append(out, referenceFinding(f))
	return out
}

// Exports returns the environment changes that the findings call for, merged.
func Exports(findings []Finding) []Export {
	var all []Export
	for _, fd := range findings {
		all = append(all, fd.Exports...)
	}
	return MergeExports(all)
}

// Failed counts the findings with status Fail.
func Failed(findings []Finding) int {
	n := 0
	for _, fd := range findings {
		if fd.Status == Fail {
			n++
		}
	}
	return n
}

// Problems counts the findings that are not a pass.
func Problems(findings []Finding) int {
	n := 0
	for _, fd := range findings {
		if fd.Status != Pass {
			n++
		}
	}
	return n
}

func pass(check, format string, args ...any) Finding {
	return Finding{Check: check, Status: Pass, Message: fmt.Sprintf(format, args...)}
}

func hooksFinding(h HooksConfig, f Facts) Finding {
	switch f.HooksPath {
	case h.Path:
		return pass("hooks", "pushes run the gate (core.hooksPath is %s)", h.Path)
	case "":
		return Finding{Check: "hooks", Status: Fail, Message: "the pre-push gate is not enabled, so a push is not checked",
			Fix:     fmt.Sprintf("git config core.hooksPath %s", h.Path),
			Actions: []Action{{Kind: ActionSetGitConfig, Key: "core.hooksPath", Value: h.Path}}}
	}
	return Finding{Check: "hooks", Status: Warn,
		Message: fmt.Sprintf("core.hooksPath is %q, so the gate in %s does not run and a push is not checked", f.HooksPath, h.Path),
		Fix:     "git config --unset core.hooksPath, or call ./support/repoctl/repoctl gate from your own hook (--fix leaves your hooks alone)"}
}

func planFinding(f Facts) Finding {
	if f.PlanError == "" {
		return pass("gate-plan", "the gate can plan this working tree's changes")
	}
	return Finding{Check: "gate-plan", Status: Fail,
		Message: "the gate cannot plan this working tree's changes: " + f.PlanError,
		Fix:     "commit the file or remove it, or give a path no component owns an owner in .repoctl/components.toml; then rerun ./support/repoctl/repoctl gate --dry-run"}
}

func satisfies(p ProgramFacts, c Copy, exact bool) bool {
	if c.Version == "" {
		return false
	}
	if exact {
		return CompareVersions(c.Version, p.Required) == 0
	}
	return CompareVersions(c.Version, p.Required) >= 0
}

// shadows names a configured program with a wrong version that putting dir first on PATH would
// make the one scripts resolve, "" when it is safe to put dir first. The directory of a
// repository's environment holds several programs, and one of them being too old must not be
// traded for another's fix.
func shadows(policy Config, f Facts, dir, except string) string {
	for i, other := range f.Programs {
		if other.Name == except || other.Required == "" {
			continue
		}
		exact := policy.Programs[i].ExactFrom != nil
		for _, c := range append(append([]Copy{}, other.InRepo...), other.OnPath...) {
			if c.Directory == dir && !satisfies(other, c, exact) {
				return other.Name
			}
		}
	}
	return ""
}

func programFinding(policy Config, cfg ProgramConfig, p ProgramFacts, f Facts) Finding {
	root := f.Root
	check := cfg.Name
	if p.RequiredError != "" {
		return Finding{Check: check, Status: Fail, Message: p.RequiredError,
			Fix: "restore the pin the doctor policy (.repoctl/doctor.toml) reads"}
	}
	install := installRemedy(cfg, root)
	prepend := func(dir string) []Export { return []Export{{Name: "PATH", Value: dir, Prepend: true}} }
	need := ""
	if p.Required != "" {
		need = fmt.Sprintf(" %s%s", map[bool]string{true: "exactly ", false: ">= "}[cfg.ExactFrom != nil], p.Required)
	}
	switch {
	case p.Required == "" && len(p.OnPath) > 0:
		return pass(check, "%s is on PATH (%s)", cfg.Name, p.OnPath[0].Directory)
	case p.Required == "" && len(p.InRepo) > 0 && shadows(policy, f, p.InRepo[0].Directory, cfg.Name) != "":
		return Finding{Check: check, Status: Fail,
			Message: fmt.Sprintf("%s is installed in %s, but putting that first on PATH would hide the %s that scripts need", cfg.Name, p.InRepo[0].Directory, shadows(policy, f, p.InRepo[0].Directory, cfg.Name)),
			Fix:     fmt.Sprintf("link %s into a directory already on PATH", filepath.Join(p.InRepo[0].Directory, cfg.Name))}
	case p.Required == "" && len(p.InRepo) > 0:
		return Finding{Check: check, Status: Fail,
			Message: fmt.Sprintf("%s is installed in %s but not on PATH", cfg.Name, p.InRepo[0].Directory),
			Fix:     pathFix(p.InRepo[0].Directory), Exports: prepend(p.InRepo[0].Directory)}
	case p.Required == "":
		return missing(cfg, install, fmt.Sprintf("%s is not installed", cfg.Name))
	case len(p.OnPath) > 0 && satisfies(p, p.OnPath[0], cfg.ExactFrom != nil):
		return pass(check, "%s %s is first on PATH (%s)", cfg.Name, p.OnPath[0].Version, p.OnPath[0].Directory)
	}
	first := "none on PATH"
	if len(p.OnPath) > 0 {
		first = fmt.Sprintf("the first on PATH is %s", orUnknown(p.OnPath[0].Version))
	}
	for _, c := range append(append([]Copy{}, p.InRepo...), p.OnPath...) {
		if satisfies(p, c, cfg.ExactFrom != nil) && shadows(policy, f, c.Directory, cfg.Name) == "" {
			return Finding{Check: check, Status: Fail,
				Message: fmt.Sprintf("%s needs%s but %s; %s %s is in %s", cfg.Name, need, first, cfg.Name, c.Version, c.Directory),
				Fix:     pathFix(c.Directory), Exports: prepend(c.Directory)}
		}
	}
	return missing(cfg, install, fmt.Sprintf("%s needs%s but %s", cfg.Name, need, first))
}

func orUnknown(v string) string {
	if v == "" {
		return "a version that could not be read"
	}
	return v
}

func pathFix(dir string) string {
	return fmt.Sprintf("put it first on PATH: eval \"$(./support/repoctl/repoctl doctor --env)\", or %s", Export{Name: "PATH", Value: dir, Prepend: true}.Line())
}

func installRemedy(cfg ProgramConfig, root string) []Action {
	if len(cfg.Fix) == 0 {
		return nil
	}
	return []Action{{Kind: ActionRun, Dir: root, Argv: cfg.Fix}}
}

func missing(cfg ProgramConfig, install []Action, message string) Finding {
	f := Finding{Check: cfg.Name, Status: Fail, Message: message, Actions: install}
	var parts []string
	for _, a := range install {
		parts = append(parts, a.Describe())
	}
	if len(parts) > 0 {
		parts = append(parts, "then put it first on PATH: eval \"$(./support/repoctl/repoctl doctor --env)\"")
	}
	if cfg.Hint != "" && len(install) == 0 {
		parts = append(parts, cfg.Hint)
	}
	f.Fix = strings.Join(parts, "; ")
	if f.Fix == "" {
		f.Fix = "install " + cfg.Name
	}
	// After the repair the copy sits in a repository directory; exports are derived on the next
	// evaluation, which is why `--fix` evaluates again.
	return f
}

func installFinding(in InstallConfig, f InstallFacts, root string) Finding {
	run := Action{Kind: ActionRun, Dir: filepath.Join(root, in.CommandDirectory), Argv: in.Command}
	switch {
	case f.DirectoryExists && f.MarkerExists:
		return pass(in.Name, "%s is fully installed", in.Directory)
	case f.DirectoryExists:
		remove := Action{Kind: ActionRemoveTree, Path: filepath.Join(root, in.Directory)}
		return Finding{Check: in.Name, Status: Fail,
			Message: fmt.Sprintf("%s exists but %s does not: the install was interrupted or failed halfway", in.Directory, in.Marker),
			Fix:     remove.Describe() + " && " + run.Describe(), Actions: []Action{remove, run}}
	}
	return Finding{Check: in.Name, Status: Warn,
		Message: fmt.Sprintf("%s is not installed; the checks for the code that needs it fail until it is", in.Directory),
		Fix:     run.Describe(), Actions: []Action{run}}
}

func tempFinding(cfg TempConfig, f Facts) Finding {
	t := f.Temp
	var problems []string
	network := IsNetworkFilesystem(t.Filesystem.Type)
	crossDevice := !network && t.Filesystem.Device != 0 && t.Root.Device != 0 && t.Filesystem.Device != t.Root.Device
	if network {
		problems = append(problems, fmt.Sprintf("%s is on a network file system (%s), where Go tests and unix sockets misbehave", t.Dir, t.Filesystem.Type))
	}
	if crossDevice {
		problems = append(problems, fmt.Sprintf("the repository and %s are on different file systems, which breaks a local clone (hard links cannot cross them)", t.Dir))
	}
	if len(t.Dir) > cfg.MaxPathLength {
		problems = append(problems, fmt.Sprintf("%s is %d characters, and a unix socket under it can exceed the 104-byte socket path limit", t.Dir, len(t.Dir)))
	}
	if len(problems) == 0 {
		return pass("tmpdir", "%s is local (%s)", t.Dir, orUnknownType(t.Filesystem.Type))
	}
	fd := Finding{Check: "tmpdir", Status: Warn, Message: strings.Join(problems, "; ")}
	// A candidate is offered only when it removes every problem found: it is local, it is on the
	// repository's file system when the file systems were the problem, and its path is short.
	c := t.Candidate
	solves := c != nil && len(c.Path) <= cfg.MaxPathLength && !(crossDevice && !sameDevice(c.Filesystem, t.Root)) && c.Path != t.Dir
	if !solves {
		hint := "set TMPDIR to a directory on a local disk"
		if vars := cfg.candidateVariables(); len(vars) > 0 {
			hint = fmt.Sprintf("set %s to a directory on a local disk, then run: eval \"$(./support/repoctl/repoctl doctor --env)\"", vars[0])
		}
		fd.Fix = hint
		return fd
	}
	ex := Export{Name: "TMPDIR", Value: c.Path}
	mk := Action{Kind: ActionMakeDirectory, Path: c.Path}
	fd.Fix = mk.Describe() + " && " + ex.Line()
	fd.Actions = []Action{mk}
	fd.Exports = []Export{ex}
	return fd
}

func orUnknownType(t string) string {
	if t == "" {
		return "type unknown"
	}
	return t
}

func envFlagFinding(flag EnvFlagConfig, f Facts) Finding {
	check := flag.Name
	if flag.LinkedWorktreeOnly && !f.LinkedWorktree {
		return pass(check, "%s is not needed outside a linked worktree", flag.Flag)
	}
	current := f.Env[flag.Name]
	for _, word := range strings.Fields(current) {
		if word == flag.Flag {
			return pass(check, "%s contains %s", flag.Name, flag.Flag)
		}
	}
	value := strings.TrimSpace(current + " " + flag.Flag)
	ex := Export{Name: flag.Name, Value: value}
	msg := fmt.Sprintf("%s lacks %s", flag.Name, flag.Flag)
	if flag.Reason != "" {
		msg += ": " + flag.Reason
	}
	return Finding{Check: check, Status: Fail, Message: msg, Fix: ex.Line(), Exports: []Export{ex}}
}

func referenceFinding(f Facts) Finding {
	var gone []string
	for _, r := range f.References {
		if !r.Exists {
			gone = append(gone, fmt.Sprintf("%s names %s", r.File, r.Path))
		}
	}
	if len(gone) == 0 {
		return pass("instructions", "every script the instruction files name exists")
	}
	return Finding{Check: "instructions", Status: Fail,
		Message: "an instruction file names a file that does not exist: " + strings.Join(gone, "; "),
		Fix:     "point the instruction at the file that replaced it, or remove the line"}
}
