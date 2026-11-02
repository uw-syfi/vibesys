package doctor

import (
	"fmt"
	"io"
	"path/filepath"
	"strings"
)

// Options are the flags of `repoctl doctor`.
type Options struct {
	// Fix performs the safe repairs, then checks again.
	Fix bool
	// Env prints only the exports a shell should evaluate, on Out, and nothing else.
	Env bool
}

// Run gathers, evaluates, optionally repairs, and reports on out. It returns an error naming the
// number of failed checks, so the command exits non-zero; warnings do not fail it.
func Run(cfg Config, root string, host Host, opts Options, out io.Writer) error {
	findings := Evaluate(cfg, Gather(cfg, root, host))
	if opts.Env {
		for _, e := range Exports(findings) {
			fmt.Fprintln(out, e.Line())
		}
		return nil
	}
	if opts.Fix {
		report(out, findings)
		repaired, err := Apply(host, root, findings, out)
		if err != nil {
			return err
		}
		if repaired == 0 {
			fmt.Fprintln(out, "\ndoctor: nothing to repair automatically")
			return verdict(findings)
		}
		fmt.Fprintln(out, "\ndoctor: checking again")
		findings = Evaluate(cfg, Gather(cfg, root, host))
	}
	report(out, findings)
	if exports := Exports(findings); len(exports) > 0 {
		fmt.Fprintln(out, "\ndoctor: these environment changes complete the fixes; apply them to this shell with")
		fmt.Fprintln(out, "  eval \"$(./support/repoctl/repoctl doctor --env)\"")
	}
	return verdict(findings)
}

func verdict(findings []Finding) error {
	if n := Failed(findings); n > 0 {
		return fmt.Errorf("doctor: %d check(s) failed", n)
	}
	return nil
}

func report(out io.Writer, findings []Finding) {
	for _, fd := range findings {
		fmt.Fprintf(out, "doctor: %-4s %-22s %s\n", fd.Status, fd.Check, fd.Message)
		if fd.Fix != "" {
			fmt.Fprintf(out, "              fix: %s\n", fd.Fix)
		}
	}
	fmt.Fprintf(out, "doctor: %d checks, %d failed, %d warnings\n", len(findings), Failed(findings), Problems(findings)-Failed(findings))
}

// Apply performs the Actions of every finding that is not a pass, in order, and returns how many
// findings it repaired. A finding's actions stop at its first error, which Apply returns.
func Apply(host Host, root string, findings []Finding, out io.Writer) (int, error) {
	repaired := 0
	for _, fd := range findings {
		if fd.Status == Pass || len(fd.Actions) == 0 {
			continue
		}
		fmt.Fprintf(out, "doctor: repairing %s\n", fd.Check)
		for _, a := range fd.Actions {
			if err := guard(root, a); err != nil {
				return repaired, fmt.Errorf("repair %s: %w", fd.Check, err)
			}
			fmt.Fprintf(out, "  %s\n", a.Describe())
			if err := host.Execute(a); err != nil {
				return repaired, fmt.Errorf("repair %s: %s: %w", fd.Check, a.Describe(), err)
			}
		}
		repaired++
	}
	return repaired, nil
}

// guard refuses an action that could reach outside what the policy owns: a deletion must name a
// path strictly inside the repository root.
func guard(root string, a Action) error {
	switch a.Kind {
	case ActionRemoveTree:
		rel, err := filepath.Rel(root, filepath.Clean(a.Path))
		if err != nil || rel == "." || rel == ".." || strings.HasPrefix(rel, ".."+string(filepath.Separator)) || filepath.IsAbs(rel) {
			return fmt.Errorf("refusing to delete %q: not inside the repository", a.Path)
		}
	case ActionRun:
		if len(a.Argv) == 0 {
			return fmt.Errorf("empty command")
		}
	case ActionMakeDirectory:
		if !filepath.IsAbs(a.Path) {
			return fmt.Errorf("refusing to create relative directory %q", a.Path)
		}
	case ActionSetGitConfig:
		if a.Key == "" {
			return fmt.Errorf("empty git config key")
		}
	default:
		return fmt.Errorf("unknown action %q", a.Kind)
	}
	return nil
}
