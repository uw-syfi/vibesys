package devlaunch

import (
	"errors"
	"fmt"
	"io"
	"path"
	"path/filepath"
	"regexp"
	"sort"
	"strings"
	"time"
)

// Cmd is one process to run. Stdin is fed and closed. Passthrough connects the child to the
// terminal, for the app, whose output the user watches; otherwise stdout is returned.
type Cmd struct {
	Dir   string
	Argv  []string
	Stdin []byte
	// Env is added to the inherited environment, as NAME=value.
	Env         []string
	Passthrough bool
	Timeout     time.Duration
}

// Runner runs a Cmd to its end and returns its stdout. The error names the command and, for a
// failed one, what it printed on stderr.
type Runner interface {
	Run(Cmd) ([]byte, error)
}

// Clock is the shell's only source of time.
type Clock interface {
	Now() time.Time
	Sleep(time.Duration)
}

// Files reads and writes under the repository root; paths are repository-relative.
type Files interface {
	Glob(pattern string) ([]string, error)
	Read(path string) ([]byte, error)
	// ReadHome reads a file named outside the repository; a leading `~` is the home directory.
	ReadHome(path string) ([]byte, error)
	Exists(path string) bool
	Write(path string, data []byte) error
}

// Env is everything the shell touches.
type Env struct {
	// Root is the absolute repository root.
	Root   string
	Runner Runner
	Clock  Clock
	Files  Files
	Out    io.Writer
}

var sshAlias = regexp.MustCompile(`^[A-Za-z0-9_.@][A-Za-z0-9_.@-]*$`)

// Run gathers the facts, plans, and performs the plan. The app step returns when the app closes.
func Run(env Env, cfg Config, opts Options) error {
	if opts.Machine != "" && !sshAlias.MatchString(opts.Machine) {
		return fmt.Errorf("--machine %q is not a usable ssh alias (letters, digits, . _ @ -)", opts.Machine)
	}
	if opts.Checkout != "" && opts.Machine == "" {
		return errors.New("--checkout needs --machine")
	}
	if opts.Checkout != "" {
		if err := ValidateRemotePath(opts.Checkout); err != nil {
			return err
		}
	}
	s := &session{env: env, cfg: cfg, opts: opts}
	in, err := s.facts()
	if err != nil {
		return err
	}
	steps, err := PlanSteps(in)
	if err != nil {
		return err
	}
	s.in = in
	s.printf("plan: %d steps%s\n", len(steps), map[bool]string{true: " (dry run, nothing changes)", false: ""}[opts.DryRun])
	for i, st := range steps {
		s.printf("  %d. %s (%s)\n", i+1, st.Label, st.Reason)
	}
	if opts.DryRun {
		return nil
	}
	for i, st := range steps {
		s.printf("[%d/%d] %s\n", i+1, len(steps), st.Label)
		start := env.Clock.Now()
		err := s.perform(st)
		took := env.Clock.Now().Sub(start).Round(100 * time.Millisecond)
		if err != nil {
			return fmt.Errorf("step %q failed after %s: %w", st.Label, took, err)
		}
		s.printf("      done in %s\n", took)
	}
	return nil
}

type session struct {
	env  Env
	cfg  Config
	opts Options
	in   Inputs
}

func (s *session) printf(format string, args ...any) { fmt.Fprintf(s.env.Out, format, args...) }

func (s *session) facts() (Inputs, error) {
	var in Inputs
	fp, err := s.fingerprint()
	if err != nil {
		return in, err
	}
	stamp, _ := s.env.Files.Read(s.cfg.Local.InstallStamp)
	in.Install = InstallState{
		Fingerprint:    fp,
		Stamp:          strings.TrimSpace(string(stamp)),
		ModulesPresent: s.env.Files.Exists(path.Join(s.cfg.Local.ClientDirectory, "node_modules")),
	}
	if s.opts.Machine == "" {
		return in, nil
	}
	if in.Git, err = s.localGit(); err != nil {
		return in, err
	}
	remote, err := s.probeMachine()
	if err != nil {
		return in, err
	}
	in.Remote = &remote
	return in, nil
}

func (s *session) fingerprint() (string, error) {
	seen := map[string]bool{}
	var files []File
	for _, pattern := range s.cfg.Local.InstallInputs {
		matches, err := s.env.Files.Glob(pattern)
		if err != nil {
			return "", fmt.Errorf("install input %q: %w", pattern, err)
		}
		for _, m := range matches {
			if seen[m] {
				continue
			}
			seen[m] = true
			content, err := s.env.Files.Read(m)
			if err != nil {
				return "", fmt.Errorf("install input %q: %w", m, err)
			}
			files = append(files, File{m, content})
		}
	}
	return Fingerprint(files), nil
}

func (s *session) git(args ...string) (string, error) {
	out, err := s.env.Runner.Run(Cmd{Dir: s.env.Root, Argv: append([]string{"git"}, args...), Timeout: 30 * time.Second})
	return strings.TrimSpace(string(out)), err
}

func (s *session) localGit() (LocalGit, error) {
	head, err := s.git("rev-parse", "HEAD")
	if err != nil {
		return LocalGit{}, err
	}
	changes, err := s.git("status", "--porcelain", "--untracked-files=no")
	if err != nil {
		return LocalGit{}, err
	}
	return LocalGit{Head: head, Dirty: changes != ""}, nil
}

// ssh runs one command line on the machine; the caller has quoted its parts.
func (s *session) ssh(line string, stdin []byte, timeout time.Duration) ([]byte, error) {
	return s.env.Runner.Run(Cmd{
		Dir:     s.env.Root,
		Argv:    []string{"ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", s.opts.Machine, line},
		Stdin:   stdin,
		Timeout: timeout,
	})
}

func (s *session) probeMachine() (RemoteState, error) {
	var files []HostsFile
	for _, p := range s.cfg.Machine.AppHostsFiles {
		data, err := s.env.Files.ReadHome(p)
		files = append(files, HostsFile{Path: p, Data: data, Found: err == nil})
	}
	checkout, err := ResolveCheckout(s.opts.Checkout, s.opts.Machine, files)
	if err != nil {
		return RemoteState{}, err
	}
	if err := ValidateRemotePath(checkout); err != nil {
		return RemoteState{}, err
	}
	out, err := s.ssh(strings.Join(ProbeCommand(checkout), " "), nil, 60*time.Second)
	if err != nil {
		return RemoteState{}, fmt.Errorf("probing the machine's checkout: %w", err)
	}
	state, found, err := ParseProbe(string(out))
	if err != nil {
		return RemoteState{}, err
	}
	if !found {
		return RemoteState{Kind: CheckoutMissing, Tried: []string{checkout}}, nil
	}
	state.Tried = []string{checkout}
	return state, nil
}

// remoteLine is a command line that runs argv in the checkout, with the usual per-user program
// directories on PATH (a non-interactive ssh shell often lacks them).
func (s *session) remoteLine(argv ...string) string {
	script := fmt.Sprintf(`PATH="%s:$PATH"; cd "$1" || exit 1; shift; exec "$@"`, strings.Join(s.cfg.Machine.PathDirectories, ":"))
	parts := []string{"sh", "-c", shellQuote(script), "vibesys-dev", shellQuote(s.in.Remote.Checkout)}
	for _, a := range argv {
		parts = append(parts, shellQuote(a))
	}
	return strings.Join(parts, " ")
}

func (s *session) perform(st Step) error {
	local := s.cfg.Local
	switch st.Kind {
	case StepInstall:
		if _, err := s.env.Runner.Run(Cmd{Dir: local.ClientDirectory, Argv: local.InstallCommand, Passthrough: true}); err != nil {
			return err
		}
		return s.env.Files.Write(local.InstallStamp, []byte(s.in.Install.Fingerprint+"\n"))
	case StepPython:
		_, err := s.env.Runner.Run(Cmd{Dir: ".", Argv: local.PythonSyncCommand, Passthrough: true})
		return err
	case StepBuild:
		_, err := s.env.Runner.Run(Cmd{Dir: local.ClientDirectory, Argv: local.BuildCommand, Passthrough: true})
		return err
	case StepShipGit:
		url := "ssh://" + s.opts.Machine + s.in.Remote.Checkout
		if _, err := s.git("push", "--force", "--quiet", url, "HEAD:"+s.cfg.Machine.ShipRef); err != nil {
			return err
		}
		_, err := s.ssh(s.remoteLine("git", "checkout", "--detach", "--quiet", s.cfg.Machine.ShipRef), nil, 120*time.Second)
		return err
	case StepShipFiles:
		list, err := s.env.Runner.Run(Cmd{Dir: s.env.Root, Argv: []string{"git", "ls-files", "-z"}, Timeout: 30 * time.Second})
		if err != nil {
			return err
		}
		_, err = s.env.Runner.Run(Cmd{
			Dir:     s.env.Root,
			Argv:    []string{"rsync", "-a", "--from0", "--files-from=-", "./", s.opts.Machine + ":" + s.in.Remote.Checkout + "/"},
			Stdin:   list,
			Timeout: 10 * time.Minute,
		})
		return err
	case StepSync:
		_, err := s.ssh(s.remoteLine(s.cfg.Machine.SyncCommand...), nil, 10*time.Minute)
		return err
	case StepHelper:
		return s.waitForHello()
	case StepApp:
		return s.openApp()
	}
	return fmt.Errorf("unknown step %q", st.Kind)
}

const helloRequest = `{"type":"hello","request_id":"dev-1","deadline_ms":20000,"client_versions":[1,1],"app_version":"repoctl-dev"}` + "\n"

// waitForHello starts a fresh helper the way the app does (one ssh exec channel running the
// helper from the checkout) until its hello shows the shipped commit, within the policy's bound.
func (s *session) waitForHello() error {
	m := s.cfg.Machine
	want := ""
	if s.in.Remote.Kind == CheckoutGit {
		want = s.in.Git.Head
	}
	deadline := s.env.Clock.Now().Add(time.Duration(m.HelloSeconds) * time.Second)
	seen := "no reply"
	for {
		out, err := s.ssh(s.remoteLine(m.HelperCommand...), []byte(helloRequest), 40*time.Second)
		if err != nil {
			seen = err.Error()
		} else {
			first, _, _ := strings.Cut(string(out), "\n")
			h, perr := ParseHello([]byte(first))
			if perr != nil {
				seen = perr.Error()
			} else {
				var ready bool
				if ready, seen = HelloReady(h, want); ready {
					s.printf("      helper hello: %s\n", seen)
					return s.seedProfile()
				}
			}
		}
		if !s.env.Clock.Now().Add(time.Duration(m.HelloPollSeconds) * time.Second).Before(deadline) {
			return fmt.Errorf("the helper did not answer hello at commit %s within %ds; last seen: %s", orNone(want), m.HelloSeconds, seen)
		}
		s.env.Clock.Sleep(time.Duration(m.HelloPollSeconds) * time.Second)
	}
}

// seedProfile writes a hosts.json naming the machine into a profile that has none.
func (s *session) seedProfile() error {
	file := path.Join(s.cfg.Local.ProfileDirectory, "hosts.json")
	if s.env.Files.Exists(file) {
		return nil
	}
	data, err := SeedHosts(s.opts.Machine, s.in.Remote.Checkout)
	if err != nil {
		return err
	}
	return s.env.Files.Write(file, data)
}

func (s *session) openApp() error {
	profile := filepath.Join(s.env.Root, s.cfg.Local.ProfileDirectory)
	argv := make([]string, len(s.cfg.Local.AppCommand))
	for i, a := range s.cfg.Local.AppCommand {
		argv[i] = strings.ReplaceAll(a, "{profile}", profile)
	}
	var env []string
	for _, name := range sortedKeys(s.cfg.Local.AppEnv) {
		env = append(env, name+"="+strings.ReplaceAll(s.cfg.Local.AppEnv[name], "{profile}", profile))
	}
	_, err := s.env.Runner.Run(Cmd{Dir: s.cfg.Local.AppDirectory, Argv: argv, Env: env, Passthrough: true})
	return err
}

func sortedKeys(m map[string]string) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
