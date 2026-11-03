package operator_test

import (
	"errors"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
)

const slurmConfig = `
[docker]
placement = "slurm"
gpus = "all"
runtime = "nvidia"

[docker.mounts]
hf-cache = "/data/hf"

[remote]
host = "host-a"
remote_workspace_root = "/home/u/vibesys-cli"

[slurm]
partition = "gpu"
gres = "gpu:1"
srun_arguments = ["--nodelist=node-a"]
queue_timeout_seconds = 600
`

func TestASlurmConfigReadsWithItsDefaults(t *testing.T) {
	c, err := operator.Parse([]byte(slurmConfig))
	if err != nil {
		t.Fatal(err)
	}
	if c.Docker.Placement != operator.Slurm || c.Docker.GPUs != "all" || c.Docker.Runtime != "nvidia" || c.Docker.Mounts["hf-cache"] != "/data/hf" {
		t.Errorf("docker %+v", c.Docker)
	}
	if c.Remote == nil || c.Remote.Host != "host-a" || c.Remote.SSHCommand[0] != "ssh" || c.Remote.RsyncCommand[0] != "rsync" {
		t.Errorf("remote %+v", c.Remote)
	}
	if s := c.Slurm; s == nil || !s.Exclusive || s.QueueTimeout != 10*time.Minute || s.PollInterval != operator.DefaultPollInterval || s.Gres != "gpu:1" ||
		s.CPUs != operator.DefaultCPUs || s.Memory != "" {
		t.Errorf("slurm %+v", c.Slurm)
	}
	c, err = operator.Parse([]byte(strings.Replace(slurmConfig, "[slurm]", "[slurm]\ncpus = 12\nmemory = \"48G\"", 1)))
	if err != nil || c.Slurm.CPUs != 12 || c.Slurm.Memory != "48G" {
		t.Errorf("slurm %+v, %v; want 12 CPUs and 48G", c.Slurm, err)
	}
}

func TestNoConfigIsLocalDockerWithNoGPU(t *testing.T) {
	for _, text := range []string{"", "[docker]\n"} {
		c, err := operator.Parse([]byte(text))
		if err != nil || c.Docker.Placement != operator.Local || c.Docker.GPUs != "" || c.Remote != nil || c.Slurm != nil {
			t.Errorf("%q: %+v, %v", text, c, err)
		}
	}
	c, err := operator.Load(filepath.Join(t.TempDir(), "absent.toml"), false)
	if err != nil || c.Docker.Placement != operator.Local {
		t.Errorf("a missing default file: %+v, %v", c, err)
	}
	if _, err := operator.Load(filepath.Join(t.TempDir(), "absent.toml"), true); err == nil {
		t.Errorf("a named file that is missing was read as the default")
	}
}

// Every mistake is refused, naming the key: a typo must never be a setting quietly left out.
func TestAMistakeIsRefusedNamingTheKey(t *testing.T) {
	cases := map[string]string{
		"an unknown key":                    "[docker]\nplacment = \"ssh\"\n",
		"an unknown section":                "[sluurm]\n",
		"an unknown placement":              "[docker]\nplacement = \"k8s\"\n",
		"ssh with no remote":                "[docker]\nplacement = \"ssh\"\n",
		"slurm with no slurm":               strings.Replace(slurmConfig, "[slurm]", "[ignored]", 1),
		"a remote that is unused":           "[remote]\nhost = \"h\"\nremote_workspace_root = \"/r\"\n",
		"a host with an option":             strings.Replace(slurmConfig, `"host-a"`, `"-oProxyCommand=x"`, 1),
		"a relative workspace":              strings.Replace(slurmConfig, `"/home/u/vibesys-cli"`, `"vibesys-cli"`, 1),
		"the root as workspace":             strings.Replace(slurmConfig, `"/home/u/vibesys-cli"`, `"/"`, 1),
		"a relative mount":                  strings.Replace(slurmConfig, `"/data/hf"`, `"data/hf"`, 1),
		"a mount name with a slash":         strings.Replace(slurmConfig, "hf-cache =", "\"hf/cache\" =", 1),
		"a runtime with no GPU":             "[docker]\nruntime = \"nvidia\"\n",
		"a gpus value with a space":         "[docker]\ngpus = \"all --privileged\"\n",
		"an srun argument not a flag":       strings.Replace(slurmConfig, `"--nodelist=node-a"`, `"node-a"`, 1),
		"a negative queue timeout":          strings.Replace(slurmConfig, "600", "-1", 1),
		"a wrong type":                      "[docker]\ngpus = 1\n",
		"an unused slurm on ssh":            strings.Replace(slurmConfig, `placement = "slurm"`, `placement = "ssh"`, 1),
		"an empty ssh command":              strings.Replace(slurmConfig, "[slurm]", "ssh_command = []\n[slurm]", 1),
		"a gres that is not a value":        strings.Replace(slurmConfig, `"gpu:1"`, `"gpu:1 --x"`, 1),
		"an unknown key in a sub-table":     strings.Replace(slurmConfig, "[slurm]", "[slurm]\nnodes = 1", 1),
		"no CPUs":                           strings.Replace(slurmConfig, "[slurm]", "[slurm]\ncpus = 0", 1),
		"a memory with no unit":             strings.Replace(slurmConfig, "[slurm]", "[slurm]\nmemory = \"32\"", 1),
		"a memory Docker reads another way": strings.Replace(slurmConfig, "[slurm]", "[slurm]\nmemory = \"32GB\"", 1),
	}
	for name, text := range cases {
		_, err := operator.Parse([]byte(text))
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
			t.Errorf("%s: %v, want an invalid input", name, err)
		}
	}
}

func TestThePathFollowsTheVariablesAndXDG(t *testing.T) {
	env := func(m map[string]string) func(string) string { return func(k string) string { return m[k] } }
	if p := operator.Path(env(map[string]string{operator.EnvConfig: "/x/c.toml", "XDG_CONFIG_HOME": "/cfg"}), "/home/u"); p != "/x/c.toml" {
		t.Errorf("named: %s", p)
	}
	if p := operator.Path(env(map[string]string{"XDG_CONFIG_HOME": "/cfg"}), "/home/u"); p != "/cfg/vibesys/cli.toml" {
		t.Errorf("xdg: %s", p)
	}
	if p := operator.Path(env(map[string]string{"XDG_CONFIG_HOME": "rel"}), "/home/u"); p != "/home/u/.config/vibesys/cli.toml" {
		t.Errorf("default: %s", p)
	}
	file := filepath.Join(t.TempDir(), "cli.toml")
	if err := os.WriteFile(file, []byte("[docker]\ngpus = \"all\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if c, err := operator.Load(file, true); err != nil || c.Docker.GPUs != "all" {
		t.Errorf("load: %+v, %v", c, err)
	}
}
