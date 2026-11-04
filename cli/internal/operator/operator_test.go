package operator_test

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
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

// A file of the earlier shape is the one placement "default", its GPUs and mounts its resources.
func TestAConfigOfTheEarlierShapeIsThePlacementDefault(t *testing.T) {
	c, err := operator.Parse([]byte(slurmConfig))
	if err != nil {
		t.Fatal(err)
	}
	want := &cliv1.OperatorConfig{Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{"default": {
		Kind: cliv1.PlacementKind_PLACEMENT_KIND_SLURM, MaxJobs: 1,
		Remote: &cliv1.RemoteSettings{Host: "host-a", RemoteWorkspaceRoot: "/home/u/vibesys-cli"},
		Slurm:  &cliv1.SlurmSettings{Partition: "gpu", Gres: "gpu:1", SrunArguments: []string{"--nodelist=node-a"}, QueueTimeoutSeconds: 600},
		Resources: &cliv1.Resources{
			Gpu:    &cliv1.GpuResource{Devices: "all", Runtime: "nvidia"},
			Mounts: map[string]string{"hf-cache": "/data/hf"},
		},
	}}}
	if !proto.Equal(c, want) {
		t.Errorf("Parse =\n%v\nwant\n%v", c, want)
	}
	c, err = operator.Parse([]byte(strings.Replace(slurmConfig, "[slurm]", "[slurm]\ncpus = 12\nmemory = \"48G\"\nshared_gres = \"shard:1\"\nexclusive = false", 1)))
	if s := c.GetPlacements()["default"].GetSlurm(); err != nil || s.GetCpus() != 12 || s.GetMemory() != "48G" || s.GetSharedGres() != "shard:1" || s.Exclusive == nil || *s.Exclusive {
		t.Errorf("slurm %v, %v; want 12 CPUs, 48G and not exclusive", s, err)
	}
}

func TestNoConfigIsOneLocalPlacementWithNoResource(t *testing.T) {
	want := &cliv1.OperatorConfig{Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{"default": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, MaxJobs: 1}}}
	for _, text := range []string{"", "[docker]\n", "[docker]\nplacement = \"local\"\n"} {
		c, err := operator.Parse([]byte(text))
		if err != nil || !proto.Equal(c, want) {
			t.Errorf("%q: %v, %v", text, c, err)
		}
	}
	c, err := operator.Load(filepath.Join(t.TempDir(), "absent.toml"), false)
	if err != nil || !proto.Equal(c, want) {
		t.Errorf("a missing default file: %v, %v", c, err)
	}
	if _, err := operator.Load(filepath.Join(t.TempDir(), "absent.toml"), true); err == nil {
		t.Errorf("a named file that is missing was read as the default")
	}
}

const placementsConfig = `
[placements.local]
kind = "local"
max_jobs = 4
[placements.local.resources.kubernetes]
kubeconfig = "/home/u/.kube/kind-a"
registry = "localhost:5001"
builder = "tcp://127.0.0.1:1234"
network = "host"
[placements.local.resources.mounts]
models = "/data/models"

[placements.a100]
kind = "slurm"
[placements.a100.remote]
host = "host-a"
remote_workspace_root = "/scratch/u/vibesys-cli"
[placements.a100.slurm]
partition = "a100"
gres = "gpu:1"
[placements.a100.resources.gpu]
devices = "all"
`

func TestPlacementsAreReadByName(t *testing.T) {
	c, err := operator.Parse([]byte(placementsConfig))
	if err != nil {
		t.Fatal(err)
	}
	want := &cliv1.OperatorConfig{Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{
		"local": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, MaxJobs: 4, Resources: &cliv1.Resources{
			Kubernetes: &cliv1.KubernetesResource{Kubeconfig: "/home/u/.kube/kind-a", Registry: "localhost:5001", Builder: "tcp://127.0.0.1:1234", Network: "host"},
			Mounts:     map[string]string{"models": "/data/models"},
		}},
		"a100": {
			Kind: cliv1.PlacementKind_PLACEMENT_KIND_SLURM, MaxJobs: 1,
			Remote:    &cliv1.RemoteSettings{Host: "host-a", RemoteWorkspaceRoot: "/scratch/u/vibesys-cli"},
			Slurm:     &cliv1.SlurmSettings{Partition: "a100", Gres: "gpu:1"},
			Resources: &cliv1.Resources{Gpu: &cliv1.GpuResource{Devices: "all"}},
		},
	}}
	if !proto.Equal(c, want) {
		t.Errorf("Parse =\n%v\nwant\n%v", c, want)
	}
}

// Every mistake is refused, naming the key: a typo must never be a setting quietly left out.
func TestAMistakeIsRefusedNamingTheKey(t *testing.T) {
	cases := map[string]struct{ text, names string }{
		"an unknown key":                       {"[docker]\nplacment = \"ssh\"\n", "docker.placment"},
		"an unknown section":                   {"[sluurm]\n", "sluurm"},
		"an unknown placement":                 {"[docker]\nplacement = \"k8s\"\n", "docker.placement"},
		"ssh with no remote":                   {"[docker]\nplacement = \"ssh\"\n", "[remote]"},
		"slurm with no slurm":                  {strings.Replace(slurmConfig, "[slurm]", "[ignored]", 1), "ignored"},
		"a remote that is unused":              {"[remote]\nhost = \"h\"\nremote_workspace_root = \"/r\"\n", "[remote]"},
		"a host with an option":                {strings.Replace(slurmConfig, `"host-a"`, `"-oProxyCommand=x"`, 1), "host"},
		"a relative workspace":                 {strings.Replace(slurmConfig, `"/home/u/vibesys-cli"`, `"vibesys-cli"`, 1), "remote_workspace_root"},
		"the root as workspace":                {strings.Replace(slurmConfig, `"/home/u/vibesys-cli"`, `"/"`, 1), "remote_workspace_root"},
		"a relative mount":                     {strings.Replace(slurmConfig, `"/data/hf"`, `"data/hf"`, 1), "mounts"},
		"a mount name with a slash":            {strings.Replace(slurmConfig, "hf-cache =", "\"hf/cache\" =", 1), "mounts"},
		"a runtime with no GPU":                {"[docker]\nruntime = \"nvidia\"\n", "docker.runtime"},
		"a gpus value with a space":            {"[docker]\ngpus = \"all --privileged\"\n", "gpus"},
		"an srun argument not a flag":          {strings.Replace(slurmConfig, `"--nodelist=node-a"`, `"node-a"`, 1), "srun_arguments"},
		"a negative queue timeout":             {strings.Replace(slurmConfig, "600", "-1", 1), "queue_timeout_seconds"},
		"a wrong type":                         {"[docker]\ngpus = 1\n", "docker.gpus"},
		"an unused slurm on ssh":               {strings.Replace(slurmConfig, `placement = "slurm"`, `placement = "ssh"`, 1), "[slurm]"},
		"an empty ssh program":                 {strings.Replace(slurmConfig, "[slurm]", "ssh_command = [\"\"]\n[slurm]", 1), "ssh_command"},
		"a gres that is not a value":           {strings.Replace(slurmConfig, `"gpu:1"`, `"gpu:1 --x"`, 1), "gres"},
		"an unknown key in a sub-table":        {strings.Replace(slurmConfig, "[slurm]", "[slurm]\nnodes = 1", 1), "slurm.nodes"},
		"a shared gres that is not a value":    {strings.Replace(slurmConfig, "[slurm]", "[slurm]\nshared_gres = \"shard:1 --x\"", 1), "shared_gres"},
		"no CPUs":                              {strings.Replace(slurmConfig, "[slurm]", "[slurm]\ncpus = 0", 1), "cpus"},
		"a memory with no unit":                {strings.Replace(slurmConfig, "[slurm]", "[slurm]\nmemory = \"32\"", 1), "memory"},
		"a memory Docker reads another way":    {strings.Replace(slurmConfig, "[slurm]", "[slurm]\nmemory = \"32GB\"", 1), "memory"},
		"both shapes":                          {placementsConfig + "[docker]\ngpus = \"all\"\n", "[placements.*]"},
		"a placement with no kind":             {"[placements.x]\nmax_jobs = 2\n", "kind"},
		"a placement name in upper case":       {"[placements.X]\nkind = \"local\"\n", "placements"},
		"a kind in upper case":                 {"[placements.x]\nkind = \"LOCAL\"\n", "placements.x.kind"},
		"a negative max_jobs":                  {"[placements.x]\nkind = \"local\"\nmax_jobs = -1\n", "placements.x.max_jobs"},
		"an unknown resource":                  {"[placements.x]\nkind = \"local\"\n[placements.x.resources.tpu]\n", "placements.x.resources.tpu"},
		"a kubeconfig that is relative":        {"[placements.x]\nkind = \"local\"\n[placements.x.resources.kubernetes]\nkubeconfig = \"kube\"\n", "kubeconfig"},
		"a cluster with no kubeconfig":         {"[placements.x]\nkind = \"local\"\n[placements.x.resources.kubernetes]\nnetwork = \"host\"\n", "kubeconfig"},
		"a network with a space":               {"[placements.x]\nkind = \"local\"\n[placements.x.resources.kubernetes]\nkubeconfig = \"/k\"\nnetwork = \"a b\"\n", "network"},
		"a gpu with no devices":                {"[placements.x]\nkind = \"local\"\n[placements.x.resources.gpu]\nruntime = \"nvidia\"\n", "devices"},
		"a local placement with a remote":      {"[placements.x]\nkind = \"local\"\n[placements.x.remote]\nhost = \"h\"\nremote_workspace_root = \"/r\"\n", "[placements.x.remote]"},
		"an ssh placement with no remote":      {"[placements.x]\nkind = \"ssh\"\n", "[placements.x.remote]"},
		"no jobs at once":                      {"[scheduler]\nmax_jobs = 0\n", "scheduler.max_jobs"},
		"an empty policy":                      {"[scheduler]\npolicy = \"\"\n", "scheduler.policy"},
		"an unknown scheduler key":             {"[scheduler]\nmax_job = 2\n", "scheduler.max_job"},
		"jobs at once on a remote engine":      {"[scheduler]\nmax_jobs = 2\n" + slurmConfig, "scheduler.max_jobs"},
		"jobs at once on a remote placement":   {strings.Replace(sshPlacement, "kind = \"ssh\"", "kind = \"ssh\"\nmax_jobs = 2", 1), "placements.x.max_jobs"},
		"a scheduler max_jobs with placements": {"[scheduler]\nmax_jobs = 2\n" + placementsConfig, "scheduler.max_jobs"},
	}
	for name, c := range cases {
		_, err := operator.Parse([]byte(c.text))
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, c.names) {
			t.Errorf("%s: %v, want an invalid input naming %q", name, err, c.names)
		}
	}
}

// sshPlacement is one placement of the ssh kind.
const sshPlacement = `
[placements.x]
kind = "ssh"
[placements.x.remote]
host = "host-a"
remote_workspace_root = "/home/u/vibesys-cli"
`

// The scheduler runs one job at a time on each placement, the oldest first, unless the config says
// otherwise. The earlier shape's scheduler.max_jobs is the max_jobs of its one placement.
func TestTheSchedulerDefaultsToOneJobAtATime(t *testing.T) {
	type want struct {
		policy  string
		maxJobs uint32
	}
	for text, w := range map[string]want{
		"":              {"fifo", 1},
		slurmConfig:     {"fifo", 1},
		"[scheduler]\n": {"fifo", 1},
		"[scheduler]\nmax_jobs = 4\npolicy = \"fair-share\"\n":                                         {"fair-share", 4},
		"[scheduler]\nmax_jobs = 3\n[docker]\ngpus = \"all\"\n":                                        {"fifo", 3},
		"[scheduler]\npolicy = \"fair-share\"\n[placements.default]\nkind = \"local\"\nmax_jobs = 2\n": {"fair-share", 2},
	} {
		c, err := operator.Parse([]byte(text))
		if err != nil {
			t.Errorf("%q: %v", text, err)
			continue
		}
		if got := c.GetScheduler().GetPolicy(); got != w.policy || c.GetScheduler().MaxJobs != nil {
			t.Errorf("%q: scheduler %v, want policy %q and no max_jobs", text, c.GetScheduler(), w.policy)
		}
		if got := c.GetPlacements()[operator.DefaultPlacement].GetMaxJobs(); got != w.maxJobs {
			t.Errorf("%q: max_jobs %d, want %d", text, got, w.maxJobs)
		}
	}
	c, err := operator.Load(filepath.Join(t.TempDir(), "absent.toml"), false)
	if err != nil || c.GetScheduler().GetPolicy() != "fifo" || c.GetPlacements()[operator.DefaultPlacement].GetMaxJobs() != 1 {
		t.Errorf("a missing default file: %v, %v", c, err)
	}
}

// A placement of any name, kind and resources, written in the file, reads back as written, and a
// key put anywhere that the schema has no field for is refused, naming its whole path.
func TestAnyPlacementsReadAsWrittenAndAnUnknownKeyAnywhereIsNamed(t *testing.T) {
	r := rand.New(rand.NewPCG(11, 12))
	kinds := []cliv1.PlacementKind{cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, cliv1.PlacementKind_PLACEMENT_KIND_SSH, cliv1.PlacementKind_PLACEMENT_KIND_SLURM}
	for range 300 {
		want := &cliv1.OperatorConfig{Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{}}
		var text strings.Builder
		var tables []string // every table written, where an unknown key can go
		for i := range 1 + r.IntN(4) {
			name := fmt.Sprintf("%s%d", []string{"p", "local", "a100", "x_y-"}[r.IntN(4)], i)
			p := &cliv1.Placement{Kind: kinds[r.IntN(3)], MaxJobs: uint32(r.IntN(5))}
			if p.Kind != cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
				// A remote placement runs one job at a time.
				p.MaxJobs = uint32(r.IntN(2))
			}
			fmt.Fprintf(&text, "[placements.%s]\nkind = %q\n", name, strings.ToLower(strings.TrimPrefix(p.Kind.String(), "PLACEMENT_KIND_")))
			if p.MaxJobs > 0 {
				fmt.Fprintf(&text, "max_jobs = %d\n", p.MaxJobs)
			} else {
				p.MaxJobs = 1
			}
			tables = append(tables, "placements."+name)
			if p.Kind != cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
				p.Remote = &cliv1.RemoteSettings{Host: "host-a", RemoteWorkspaceRoot: "/w"}
				fmt.Fprintf(&text, "[placements.%s.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/w\"\n", name)
				tables = append(tables, "placements."+name+".remote")
			}
			if p.Kind == cliv1.PlacementKind_PLACEMENT_KIND_SLURM {
				p.Slurm = &cliv1.SlurmSettings{Partition: "gpu"}
				fmt.Fprintf(&text, "[placements.%s.slurm]\npartition = \"gpu\"\n", name)
				tables = append(tables, "placements."+name+".slurm")
			}
			if r.IntN(2) == 0 {
				p.Resources = &cliv1.Resources{}
				if r.IntN(2) == 0 {
					p.Resources.Gpu = &cliv1.GpuResource{Devices: "device=0"}
					fmt.Fprintf(&text, "[placements.%s.resources.gpu]\ndevices = \"device=0\"\n", name)
					tables = append(tables, "placements."+name+".resources.gpu")
				}
				if r.IntN(2) == 0 {
					p.Resources.Kubernetes = &cliv1.KubernetesResource{Kubeconfig: "/k/" + name, Network: "host"}
					fmt.Fprintf(&text, "[placements.%s.resources.kubernetes]\nkubeconfig = \"/k/%s\"\nnetwork = \"host\"\n", name, name)
					tables = append(tables, "placements."+name+".resources.kubernetes")
				}
				if r.IntN(2) == 0 {
					p.Resources.Mounts = map[string]string{"m": "/data/m"}
					fmt.Fprintf(&text, "[placements.%s.resources.mounts]\nm = \"/data/m\"\n", name)
				}
				if p.Resources.Gpu == nil && p.Resources.Kubernetes == nil && p.Resources.Mounts == nil {
					fmt.Fprintf(&text, "[placements.%s.resources]\n", name)
					tables = append(tables, "placements."+name+".resources")
				}
			}
			want.Placements[name] = p
		}
		got, err := operator.Parse([]byte(text.String()))
		if err != nil || !proto.Equal(got, want) {
			t.Fatalf("%s\nread as %v, %v\nwant %v", text.String(), got, err, want)
		}
		table := tables[r.IntN(len(tables))]
		bad := strings.Replace(text.String(), "["+table+"]\n", "["+table+"]\nbogus_key = 1\n", 1)
		_, err = operator.Parse([]byte(bad))
		var f *core.Failure
		if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(f.Message, `"`+table+`.bogus_key"`) {
			t.Fatalf("%s\n: %v, want a refusal naming %s.bogus_key", bad, err, table)
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
	if c, err := operator.Load(file, true); err != nil || c.GetPlacements()["default"].GetResources().GetGpu().GetDevices() != "all" {
		t.Errorf("load: %v, %v", c, err)
	}
}
