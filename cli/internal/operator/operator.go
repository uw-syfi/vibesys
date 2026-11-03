// Package operator reads the operator config: the settings of the machine that runs vibesys-cli's
// jobs, which belong to whoever runs the CLI and never to a task. They say where the Docker engine
// is (this machine, another one over ssh, or another one inside a Slurm allocation), which GPUs a
// container gets, and which of the machine's directories every container sees read-only.
//
// The file is TOML. Unknown keys and sections a placement does not use are refused, naming the key,
// so a typo is never a setting silently left out. No file is the default: docker jobs run on this
// machine with no GPU.
package operator

import (
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path"
	"regexp"
	"sort"
	"time"

	"github.com/BurntSushi/toml"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment/docker"
)

// EnvConfig names the variable that points at another operator config file.
const EnvConfig = "VIBESYS_CLI_CONFIG"

// Placement is where the Docker engine runs containers.
type Placement string

// The placements.
const (
	// Local is the Docker engine of this machine.
	Local Placement = "local"
	// SSH is the Docker engine of another machine, reached by running docker over ssh.
	SSH Placement = "ssh"
	// Slurm is SSH with every container, and every image build, inside an allocation of the
	// machine's Slurm.
	Slurm Placement = "slurm"
)

// Defaults for what the config leaves out.
const (
	DefaultPollInterval = 5 * time.Second
	DefaultQueueTimeout = 24 * time.Hour
	// DefaultCPUs is how many CPUs a build or an unmeasured container asks for.
	DefaultCPUs = 8
)

// Config is the operator config.
type Config struct {
	Docker Docker
	// Remote is set for the ssh and slurm placements.
	Remote *Remote
	// Slurm is set for the slurm placement.
	Slurm *SlurmConfig
}

// Docker is how containers are given what the machine has.
type Docker struct {
	Placement Placement
	// GPUs is a docker --gpus value; empty passes none.
	GPUs string
	// Runtime, when set, passes the GPUs with --runtime and NVIDIA_VISIBLE_DEVICES instead.
	Runtime string
	// Mounts are read-only directories of the engine's machine, by name.
	Mounts map[string]string
}

// Remote is the machine the engine runs on, for the ssh and slurm placements.
type Remote struct {
	// Host is an ssh destination or an alias of the user's ssh config.
	Host string
	// SSHCommand and RsyncCommand are the programs, with arguments, that reach it.
	SSHCommand   []string
	RsyncCommand []string
	// WorkspaceRoot is an absolute directory there that holds the copies of runs' directories and
	// the caches.
	WorkspaceRoot string
}

// SlurmConfig is how an allocation is asked for.
type SlurmConfig struct {
	Partition string
	// Gres is what a measured container asks for; a build asks for no GRES.
	Gres string
	// SharedGres, when set, is what an unmeasured container asks for instead of Gres, such as a
	// shard of a GPU.
	SharedGres string
	// Exclusive gives a measured container, a benchmark sample, the whole node.
	Exclusive bool
	// CPUs is how many CPUs a build or an unmeasured container asks for.
	CPUs int
	// Memory, when set, is the memory a build or an unmeasured container asks for, a Slurm size.
	Memory string
	// SrunArguments are passed to srun as they are, after the ones above.
	SrunArguments []string
	// PollInterval is the pause between two reads of a job's state.
	PollInterval time.Duration
	// QueueTimeout bounds the wait for an allocation.
	QueueTimeout time.Duration
}

// Path is the operator config file: VIBESYS_CLI_CONFIG when set, otherwise vibesys/cli.toml in
// XDG_CONFIG_HOME, or in ~/.config when that is not an absolute path.
func Path(getenv func(string) string, home string) string {
	if p := getenv(EnvConfig); p != "" {
		return p
	}
	if dir := getenv("XDG_CONFIG_HOME"); dir != "" && path.IsAbs(dir) {
		return path.Join(dir, "vibesys", "cli.toml")
	}
	return path.Join(home, ".config", "vibesys", "cli.toml")
}

// Load reads the config at the path. A file that is not there is the default config, unless the
// path was named by VIBESYS_CLI_CONFIG: then it must be there.
func Load(file string, named bool) (Config, error) {
	data, err := os.ReadFile(file)
	switch {
	case err == nil:
		c, err := Parse(data)
		if err != nil {
			return Config{}, fmt.Errorf("the operator config %s: %w", file, err)
		}
		return c, nil
	case errors.Is(err, fs.ErrNotExist) && !named:
		return Config{Docker: Docker{Placement: Local}}, nil
	}
	return Config{}, &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf("reading the operator config: %v", err)}
}

// file is the TOML file.
type file struct {
	Docker *struct {
		Placement string            `toml:"placement"`
		GPUs      string            `toml:"gpus"`
		Runtime   string            `toml:"runtime"`
		Mounts    map[string]string `toml:"mounts"`
	} `toml:"docker"`
	Remote *struct {
		Host                string   `toml:"host"`
		SSHCommand          []string `toml:"ssh_command"`
		RsyncCommand        []string `toml:"rsync_command"`
		RemoteWorkspaceRoot string   `toml:"remote_workspace_root"`
	} `toml:"remote"`
	Slurm *struct {
		Partition           string   `toml:"partition"`
		Gres                string   `toml:"gres"`
		SharedGres          string   `toml:"shared_gres"`
		Exclusive           *bool    `toml:"exclusive"`
		CPUs                *int     `toml:"cpus"`
		Memory              string   `toml:"memory"`
		SrunArguments       []string `toml:"srun_arguments"`
		PollIntervalSeconds float64  `toml:"poll_interval_seconds"`
		QueueTimeoutSeconds float64  `toml:"queue_timeout_seconds"`
	} `toml:"slurm"`
}

var (
	hostPattern  = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9_.@-]*$`)
	valuePattern = regexp.MustCompile(`^[A-Za-z0-9=,:._/+-]*$`)
	runtimeName  = regexp.MustCompile(`^[a-z0-9._-]*$`)
	// memoryPattern is a size both Slurm (--mem) and Docker (memory=) read the same way.
	memoryPattern = regexp.MustCompile(`^[1-9][0-9]*[KMGT]$`)
)

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// Parse reads and checks an operator config.
func Parse(data []byte) (Config, error) {
	var f file
	meta, err := toml.Decode(string(data), &f)
	if err != nil {
		return Config{}, invalid("%v", err)
	}
	if undecoded := meta.Undecoded(); len(undecoded) > 0 {
		keys := make([]string, len(undecoded))
		for i, k := range undecoded {
			keys[i] = k.String()
		}
		sort.Strings(keys)
		return Config{}, invalid("unknown key %q", keys[0])
	}
	c := Config{Docker: Docker{Placement: Local}}
	if d := f.Docker; d != nil {
		if d.Placement != "" {
			c.Docker.Placement = Placement(d.Placement)
		}
		c.Docker.GPUs, c.Docker.Runtime, c.Docker.Mounts = d.GPUs, d.Runtime, d.Mounts
	}
	switch c.Docker.Placement {
	case Local, SSH, Slurm:
	default:
		return Config{}, invalid("docker.placement: %q is none of %q, %q and %q", c.Docker.Placement, Local, SSH, Slurm)
	}
	if !valuePattern.MatchString(c.Docker.GPUs) {
		return Config{}, invalid("docker.gpus: %q is not a --gpus value", c.Docker.GPUs)
	}
	if !runtimeName.MatchString(c.Docker.Runtime) || (c.Docker.Runtime != "" && c.Docker.GPUs == "") {
		return Config{}, invalid("docker.runtime: %q must be a runtime name, and passes GPUs only when docker.gpus names them", c.Docker.Runtime)
	}
	for name, dir := range c.Docker.Mounts {
		if err := docker.CheckMountName(name); err != nil {
			return Config{}, invalid("docker.mounts.%s: %v", name, err)
		}
		if !path.IsAbs(dir) {
			return Config{}, invalid("docker.mounts.%s: %q must be an absolute path on the engine's machine", name, dir)
		}
	}
	remote := c.Docker.Placement != Local
	switch {
	case remote && f.Remote == nil:
		return Config{}, invalid("docker.placement %q needs a [remote] section with the host", c.Docker.Placement)
	case !remote && f.Remote != nil:
		return Config{}, invalid("[remote] is set, and docker.placement %q does not use it", c.Docker.Placement)
	case c.Docker.Placement == Slurm && f.Slurm == nil:
		return Config{}, invalid("docker.placement \"slurm\" needs a [slurm] section")
	case c.Docker.Placement != Slurm && f.Slurm != nil:
		return Config{}, invalid("[slurm] is set, and docker.placement %q does not use it", c.Docker.Placement)
	}
	if r := f.Remote; r != nil {
		c.Remote = &Remote{
			Host: r.Host, SSHCommand: r.SSHCommand, RsyncCommand: r.RsyncCommand, WorkspaceRoot: r.RemoteWorkspaceRoot,
		}
		if !hostPattern.MatchString(r.Host) {
			return Config{}, invalid("remote.host: %q must be an ssh host or alias, without options or whitespace", r.Host)
		}
		if c.Remote.SSHCommand == nil {
			c.Remote.SSHCommand = []string{"ssh"}
		}
		if c.Remote.RsyncCommand == nil {
			c.Remote.RsyncCommand = []string{"rsync"}
		}
		for key, argv := range map[string][]string{"ssh_command": c.Remote.SSHCommand, "rsync_command": c.Remote.RsyncCommand} {
			if len(argv) == 0 || argv[0] == "" {
				return Config{}, invalid("remote.%s: needs a program", key)
			}
		}
		if !path.IsAbs(r.RemoteWorkspaceRoot) || path.Clean(r.RemoteWorkspaceRoot) == "/" {
			return Config{}, invalid("remote.remote_workspace_root: %q must be an absolute directory on the host, not /", r.RemoteWorkspaceRoot)
		}
	}
	if s := f.Slurm; s != nil {
		c.Slurm = &SlurmConfig{
			Partition: s.Partition, Gres: s.Gres, SharedGres: s.SharedGres, Exclusive: true, CPUs: DefaultCPUs, Memory: s.Memory, SrunArguments: s.SrunArguments,
			PollInterval: DefaultPollInterval, QueueTimeout: DefaultQueueTimeout,
		}
		if s.Exclusive != nil {
			c.Slurm.Exclusive = *s.Exclusive
		}
		if s.CPUs != nil {
			if *s.CPUs < 1 {
				return Config{}, invalid("slurm.cpus: %d is not a number of CPUs", *s.CPUs)
			}
			c.Slurm.CPUs = *s.CPUs
		}
		if s.Memory != "" && !memoryPattern.MatchString(s.Memory) {
			return Config{}, invalid("slurm.memory: %q is not a Slurm size such as 32G", s.Memory)
		}
		for key, v := range map[string]string{"partition": s.Partition, "gres": s.Gres, "shared_gres": s.SharedGres} {
			if !valuePattern.MatchString(v) {
				return Config{}, invalid("slurm.%s: %q is not a Slurm value", key, v)
			}
		}
		for i, a := range s.SrunArguments {
			if a == "" || a[0] != '-' {
				return Config{}, invalid("slurm.srun_arguments[%d]: %q is not an option", i, a)
			}
		}
		for key, seconds := range map[string]float64{"poll_interval_seconds": s.PollIntervalSeconds, "queue_timeout_seconds": s.QueueTimeoutSeconds} {
			if seconds < 0 {
				return Config{}, invalid("slurm.%s: %v is negative", key, seconds)
			}
		}
		if s.PollIntervalSeconds > 0 {
			c.Slurm.PollInterval = time.Duration(s.PollIntervalSeconds * float64(time.Second))
		}
		if s.QueueTimeoutSeconds > 0 {
			c.Slurm.QueueTimeout = time.Duration(s.QueueTimeoutSeconds * float64(time.Second))
		}
	}
	return c, nil
}
