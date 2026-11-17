package operator_test

import (
	"errors"
	"fmt"
	"math/rand/v2"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/durationpb"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/operator"
)

// v02 is the first line of every file of version 0.2.
const v02 = "version = \"0.2\"\n"

// parse reads the text as the body of a file of version 0.2.
func parse(text string) (*cliv1.OperatorConfig, error) { return operator.Parse([]byte(v02 + text)) }

const slurmConfig = `
[placements.spark]
kind = "slurm"
[placements.spark.remote]
host = "host-a"
remote_workspace_root = "/home/u/vibesys-cli"
[placements.spark.slurm]
partition = "gpu"
srun_arguments = ["--nodelist=node-a"]
queue_timeout_seconds = 600
[placements.spark.resources.gpu]
count = 8
gres_type = "a100"
runtime = "nvidia"
[placements.spark.resources.mounts]
hf-cache = "/data/hf"
`

func count(n uint32) *uint32 { return &n }

// A slurm placement is read with its machine, its Slurm settings, its pool of GPUs and its mounts,
// and runs one job at a time.
func TestASlurmPlacementIsRead(t *testing.T) {
	c, err := parse(slurmConfig)
	if err != nil {
		t.Fatal(err)
	}
	want := &cliv1.OperatorConfig{Version: "0.2", Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{"spark": {
		Kind: cliv1.PlacementKind_PLACEMENT_KIND_SLURM, MaxJobs: 1,
		Remote: &cliv1.RemoteSettings{Host: "host-a", RemoteWorkspaceRoot: "/home/u/vibesys-cli"},
		Slurm:  &cliv1.SlurmSettings{Partition: "gpu", SrunArguments: []string{"--nodelist=node-a"}, QueueTimeoutSeconds: 600},
		Resources: &cliv1.Resources{
			Gpu:    &cliv1.GpuPool{Count: count(8), GresType: "a100", Runtime: "nvidia"},
			Mounts: map[string]string{"hf-cache": "/data/hf"},
		},
	}}}
	if !proto.Equal(c, want) {
		t.Errorf("Parse =\n%v\nwant\n%v", c, want)
	}
	c, err = parse(strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\ncpus = 12\nmemory = \"48G\"", 1))
	if s := c.GetPlacements()["spark"].GetSlurm(); err != nil || s.GetCpus() != 12 || s.GetMemory() != "48G" {
		t.Errorf("slurm %v, %v; want 12 CPUs and 48G", s, err)
	}
}

func TestNoConfigIsOneLocalPlacementWithNoResource(t *testing.T) {
	want := &cliv1.OperatorConfig{Version: "0.2", Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{"default": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, MaxJobs: 1}}}
	if got := operator.Default(); !proto.Equal(got, want) {
		t.Errorf("the default: %v", got)
	}
	// A file of version 0.2 that names no placement is the same.
	for _, text := range []string{"", "[scheduler]\n", "[placements]\n"} {
		c, err := parse(text)
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
[placements.a100.resources.gpu]
count = 1
`

func TestPlacementsAreReadByName(t *testing.T) {
	c, err := parse(placementsConfig)
	if err != nil {
		t.Fatal(err)
	}
	want := &cliv1.OperatorConfig{Version: "0.2", Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{
		"local": {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, MaxJobs: 4, Resources: &cliv1.Resources{
			Kubernetes: &cliv1.KubernetesResource{Kubeconfig: "/home/u/.kube/kind-a", Registry: "localhost:5001", Builder: "tcp://127.0.0.1:1234", Network: "host"},
			Mounts:     map[string]string{"models": "/data/models"},
		}},
		"a100": {
			Kind: cliv1.PlacementKind_PLACEMENT_KIND_SLURM, MaxJobs: 1,
			Remote:    &cliv1.RemoteSettings{Host: "host-a", RemoteWorkspaceRoot: "/scratch/u/vibesys-cli"},
			Slurm:     &cliv1.SlurmSettings{Partition: "a100"},
			Resources: &cliv1.Resources{Gpu: &cliv1.GpuPool{Count: count(1)}},
		},
	}}
	if !proto.Equal(c, want) {
		t.Errorf("Parse =\n%v\nwant\n%v", c, want)
	}
}

// Every mistake is refused, naming the key: a typo must never be a setting quietly left out.
func TestAMistakeIsRefusedNamingTheKey(t *testing.T) {
	local := "[placements.x]\nkind = \"local\"\n"
	cases := map[string]struct{ text, names string }{
		"an unknown key":                              {local + "placment = \"ssh\"\n", "placements.x.placment"},
		"an unknown section":                          {"[sluurm]\n", "sluurm"},
		"an unknown kind":                             {"[placements.x]\nkind = \"k8s\"\n", "placements.x.kind"},
		"ssh with no remote":                          {"[placements.x]\nkind = \"ssh\"\n", "[placements.x.remote]"},
		"slurm with no slurm":                         {strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.ignored]", 1), "ignored"},
		"a host with an option":                       {strings.Replace(slurmConfig, `"host-a"`, `"-oProxyCommand=x"`, 1), "host"},
		"a relative workspace":                        {strings.Replace(slurmConfig, `"/home/u/vibesys-cli"`, `"vibesys-cli"`, 1), "remote_workspace_root"},
		"the root as workspace":                       {strings.Replace(slurmConfig, `"/home/u/vibesys-cli"`, `"/"`, 1), "remote_workspace_root"},
		"a relative mount":                            {strings.Replace(slurmConfig, `"/data/hf"`, `"data/hf"`, 1), "mounts"},
		"a mount name with a slash":                   {strings.Replace(slurmConfig, "hf-cache =", "\"hf/cache\" =", 1), "mounts"},
		"a runtime with a space":                      {strings.Replace(slurmConfig, `runtime = "nvidia"`, `runtime = "nvidia --privileged"`, 1), "runtime"},
		"an srun argument not a flag":                 {strings.Replace(slurmConfig, `"--nodelist=node-a"`, `"node-a"`, 1), "srun_arguments"},
		"a negative queue timeout":                    {strings.Replace(slurmConfig, "600", "-1", 1), "queue_timeout_seconds"},
		"a wrong type":                                {strings.Replace(slurmConfig, "count = 8", "count = \"8\"", 1), "placements.spark.resources.gpu.count"},
		"an unused slurm on ssh":                      {strings.Replace(strings.Replace(slurmConfig, `kind = "slurm"`, `kind = "ssh"`, 1), "gres_type = \"a100\"\n", "", 1), "[placements.spark.slurm]"},
		"an empty ssh program":                        {strings.Replace(slurmConfig, "[placements.spark.slurm]", "ssh_command = [\"\"]\n[placements.spark.slurm]", 1), "ssh_command"},
		"an unknown key in a sub-table":               {strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\nnodes = 1", 1), "placements.spark.slurm.nodes"},
		"no CPUs":                                     {strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\ncpus = 0", 1), "cpus"},
		"a memory with no unit":                       {strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\nmemory = \"32\"", 1), "memory"},
		"a memory Docker reads another way":           {strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\nmemory = \"32GB\"", 1), "memory"},
		"a GRES type that is not a word":              {strings.Replace(slurmConfig, `gres_type = "a100"`, `gres_type = "a100:2"`, 1), "gres_type"},
		"a zero stop grace":                           {"stop_grace_seconds = 0\n", "stop_grace_seconds"},
		"a stop grace the daemon's wait ends":         {"stop_grace_seconds = 241\n", "stop_grace_seconds"},
		"a placement with no kind":                    {"[placements.x]\nmax_jobs = 2\n", "kind"},
		"a placement name in upper case":              {"[placements.X]\nkind = \"local\"\n", "placements"},
		"a kind in upper case":                        {"[placements.x]\nkind = \"LOCAL\"\n", "placements.x.kind"},
		"a negative max_jobs":                         {local + "max_jobs = -1\n", "placements.x.max_jobs"},
		"an unknown resource":                         {local + "[placements.x.resources.tpu]\n", "placements.x.resources.tpu"},
		"a kubeconfig that is relative":               {local + "[placements.x.resources.kubernetes]\nkubeconfig = \"kube\"\n", "kubeconfig"},
		"a cluster with no kubeconfig":                {local + "[placements.x.resources.kubernetes]\nnetwork = \"host\"\n", "kubeconfig"},
		"a network with a space":                      {local + "[placements.x.resources.kubernetes]\nkubeconfig = \"/k\"\nnetwork = \"a b\"\n", "network"},
		"a local placement with a remote":             {local + "[placements.x.remote]\nhost = \"h\"\nremote_workspace_root = \"/r\"\n", "[placements.x.remote]"},
		"an empty policy":                             {"[scheduler]\npolicy = \"\"\n", "scheduler.policy"},
		"an unknown scheduler key":                    {"[scheduler]\nmax_job = 2\n", "scheduler.max_job"},
		"jobs at once on an ssh placement":            {strings.Replace(sshPlacement, "kind = \"ssh\"", "kind = \"ssh\"\nmax_jobs = 2", 1), "placements.x.max_jobs"},
		"a slurm placement running two jobs":          {strings.Replace(placementsConfig, "kind = \"slurm\"", "kind = \"slurm\"\nmax_jobs = 2", 1), "placements.a100.max_jobs"},
		"no GPUs in a pool":                           {local + "[placements.x.resources.gpu]\ncount = 0\n", "resources.gpu.count"},
		"more GPUs than a pool has":                   {local + "[placements.x.resources.rocm]\ncount = 65\n", "resources.rocm.count"},
		"both vendors on one placement":               {local + "[placements.x.resources.gpu]\n[placements.x.resources.rocm]\n", "placements.x.resources.rocm"},
		"a runtime of AMD GPUs":                       {local + "[placements.x.resources.rocm]\nruntime = \"nvidia\"\n", "placements.x.resources.rocm.runtime"},
		"a GRES type off a cluster":                   {local + "[placements.x.resources.gpu]\ncount = 2\ngres_type = \"a100\"\n", "placements.x.resources.gpu.gres_type"},
		"a slurm placement's GPUs with no count":      {strings.Replace(slurmConfig, "count = 8\n", "", 1), "placements.spark.resources.gpu.count"},
		"an apptainer placement's GPUs with no count": {strings.Replace(apptainerPlacement, "count = 8\n", "", 1), "placements.mi210.resources.rocm.count"},
		"two pools of this machine":                   {"[placements.a]\nkind = \"local\"\n[placements.a.resources.gpu]\ncount = 8\n[placements.b]\nkind = \"local\"\n[placements.b.resources.gpu]\ncount = 8\n", "placements.b.resources.gpu"},
		"a pool and all of this machine's GPUs":       {"[placements.a]\nkind = \"local\"\n[placements.a.resources.gpu]\n[placements.b]\nkind = \"local\"\n[placements.b.resources.gpu]\ncount = 8\n", "placements.b.resources.gpu"},
		"two pools of one ssh host":                   {sshPlacement + "[placements.x.resources.rocm]\ncount = 4\n" + strings.ReplaceAll(sshPlacement, "placements.x", "placements.y") + "[placements.y.resources.rocm]\ncount = 4\n", "placements.y.resources.rocm"},

		"an apptainer placement with no remote":     {strings.Replace(apptainerPlacement, "[placements.mi210.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/shared/vibesys\"\n", "", 1), "[placements.mi210.remote]"},
		"an apptainer placement with no slurm":      {strings.Replace(apptainerPlacement, "[placements.mi210.slurm]\npartition = \"part-a\"\n", "", 1), "[placements.mi210.slurm]"},
		"an apptainer placement with srun options":  {strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.slurm]\nsrun_arguments = [\"--qos=x\"]", 1), "submit_arguments"},
		"an apptainer placement with NVIDIA GPUs":   {strings.Replace(apptainerPlacement, "[placements.mi210.resources.rocm]", "[placements.mi210.resources.gpu]", 1), "rocm"},
		"an apptainer placement with a cluster":     {apptainerPlacement + "[placements.mi210.resources.kubernetes]\nkubeconfig = \"/k\"\n", "kubernetes"},
		"an apptainer section on a slurm placement": {strings.Replace(apptainerPlacement, "kind = \"apptainer\"", "kind = \"slurm\"", 1), "[placements.mi210.apptainer]"},
		"an unknown apptainer key":                  {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nbind = \"/x\"", 1), "placements.mi210.apptainer.bind"},
		"an empty submit command":                   {strings.Replace(apptainerPlacement, `submit_command = ["/opt/site/bin/sbatch-wrapper"]`, `submit_command = [""]`, 1), "submit_command"},
		"a submit argument not a flag":              {strings.Replace(apptainerPlacement, `"--account=acct-a"`, `"acct-a"`, 1), "submit_arguments"},
		"an apptainer program with a space":         {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nprogram = \"apptainer --debug\"", 1), "program"},
		"a relative local scratch directory":        {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nlocal_scratch_dir = \"scratch\"", 1), "local_scratch_dir"},
		"a choice of where to convert":              {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nconvert = \"node\"", 1), "placements.mi210.apptainer.convert"},
		"a negative conversion bound":               {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nconvert_timeout_seconds = -1", 1), "convert_timeout_seconds"},
		"an unknown rocm key":                       {strings.Replace(apptainerPlacement, "[placements.mi210.resources.rocm]", "[placements.mi210.resources.rocm]\nslices = 2", 1), "placements.mi210.resources.rocm.slices"},
		"a reuse limit without reuse":               {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nallocation_limit_seconds = 3600", 1), "allocation_limit_seconds"},
		"a negative idle release":                   {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nidle_release_seconds = -1", 1), "idle_release_seconds"},
		"an unknown runtime":                        {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nruntime = \"docker\"", 1), "runtime"},
		"an apptainer program under pyxis":          {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nruntime = \"pyxis\"\nprogram = \"apptainer\"", 1), "program"},
		"a negative cap":                            {strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.slurm]\nmax_time_seconds = -60", 1), "max_time_seconds"},
		"a cap of part of a minute":                 {strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.slurm]\nmax_time_seconds = 43170", 1), "max_time_seconds = 43170"},
		"a reuse limit over the cap":                {strings.Replace(strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.slurm]\nmax_time_seconds = 43200", 1), "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nidle_release_seconds = 900\nallocation_limit_seconds = 86400", 1), "allocation_limit_seconds = 86400 is longer than [placements.mi210.slurm] max_time_seconds = 43200"},
		"a cap on a slurm placement":                {strings.Replace(placementsConfig, "[placements.a100.slurm]", "[placements.a100.slurm]\nmax_time_seconds = 43200", 1), "[placements.a100.slurm] max_time_seconds"},
		"an unknown allocations key":                {local + "[placements.x.allocations]\nmax_held = 1\nmax_for = \"1h\"\nmax_hold = 2\n", "placements.x.allocations.max_hold"},
		"allocations with no max_held":              {local + "[placements.x.allocations]\nmax_for = \"1h\"\n", "max_held"},
		"allocations with no max_for":               {local + "[placements.x.allocations]\nmax_held = 1\n", "max_for"},
		"a max_for that is no duration":             {local + "[placements.x.allocations]\nmax_held = 1\nmax_for = 3600\n", "placements.x.allocations.max_for"},
		"a max_for of nothing":                      {local + "[placements.x.allocations]\nmax_held = 1\nmax_for = \"0s\"\n", "max_for"},
		"a negative max_for":                        {local + "[placements.x.allocations]\nmax_held = 1\nmax_for = \"-1h\"\n", "max_for"},
		"too many held":                             {local + "[placements.x.allocations]\nmax_held = 65\nmax_for = \"1h\"\n", "max_held"},
		"a run_idle_end under a minute":             {local + "[placements.x.allocations]\nmax_held = 1\nmax_for = \"1h\"\nrun_idle_end = \"30s\"\n", "run_idle_end"},
		"a run_idle_end that is no duration":        {local + "[placements.x.allocations]\nmax_held = 1\nmax_for = \"1h\"\nrun_idle_end = 7200\n", "placements.x.allocations.run_idle_end"},
		"a max_for over the cluster's cap":          {strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.allocations]\nmax_held = 2\nmax_for = \"24h\"\n[placements.mi210.slurm]\nmax_time_seconds = 43200", 1), "[placements.mi210.allocations] max_for = 24h0m0s is longer than [placements.mi210.slurm] max_time_seconds = 43200"},
		"a local scratch directory under pyxis":     {strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nruntime = \"pyxis\"\nlocal_scratch_dir = \"/scratch/me\"", 1), "local_scratch_dir"},
	}
	for name, c := range cases {
		if _, err := parse(c.text); !refused(err, c.names) {
			t.Errorf("%s: %v, want an invalid input naming %q", name, err, c.names)
		}
	}
}

// refused says whether err is an invalid input whose message holds every one of names.
func refused(err error, names ...string) bool {
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput {
		return false
	}
	for _, n := range names {
		if !strings.Contains(err.Error(), n) {
			return false
		}
	}
	return true
}

// A file of any version but 0.2, or of none, is refused, and so is every key of the shape before
// it, whatever the version says: each refusal names the key, the version the file must have and
// where it is written down.
func TestAFileNotOfVersion02IsRefusedNamingTheKey(t *testing.T) {
	for text, key := range map[string]string{
		"":                                   "version",
		"[placements.x]\nkind = \"local\"\n": "version",
		"version = \"0.1\"\n":                "version",
		"version = \"0.3\"\n":                "version",
		"version = 0.2\n":                    "version",
		"version = \"\"\n":                   "version",
	} {
		if _, err := operator.Parse([]byte(text)); !refused(err, key, `"0.2"`, "cli/README.md") {
			t.Errorf("%q: %v, want a refusal naming %s and version 0.2", text, err, key)
		}
	}
	legacy := map[string]string{
		"[docker]\ngpus = \"all\"\n": "docker",
		"[docker]\n":                 "docker",
		"[remote]\nhost = \"h\"\nremote_workspace_root = \"/r\"\n":                                                         "remote",
		"[slurm]\npartition = \"gpu\"\n":                                                                                   "slurm",
		"[scheduler]\nmax_jobs = 2\n":                                                                                      "scheduler.max_jobs",
		"[placements.x]\nkind = \"local\"\n[placements.x.resources.gpu]\ndevices = \"all\"\n":                              "placements.x.resources.gpu.devices",
		strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\ngres = \"gpu:8\"", 1):          "placements.spark.slurm.gres",
		strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\nshared_gres = \"shard:1\"", 1): "placements.spark.slurm.shared_gres",
		strings.Replace(slurmConfig, "[placements.spark.slurm]", "[placements.spark.slurm]\nexclusive = false", 1):         "placements.spark.slurm.exclusive",
	}
	for text, key := range legacy {
		for _, version := range []string{"", v02, "version = \"0.1\"\n"} {
			if _, err := operator.Parse([]byte(version + text)); !refused(err, key+":", `"0.2"`, "cli/README.md") {
				t.Errorf("%q: %v, want a refusal naming %s and version 0.2", version+text, err, key)
			}
		}
	}
}

// apptainerPlacement is one placement of the apptainer kind, on a cluster whose site wraps sbatch
// and requires an account on every job.
const apptainerPlacement = `
[placements.mi210]
kind = "apptainer"
[placements.mi210.remote]
host = "host-a"
remote_workspace_root = "/shared/vibesys"
[placements.mi210.slurm]
partition = "part-a"
[placements.mi210.apptainer]
submit_command = ["/opt/site/bin/sbatch-wrapper"]
submit_arguments = ["--account=acct-a"]
[placements.mi210.resources.rocm]
count = 8
[placements.mi210.resources.mounts]
hf = "/shared/hf"
`

// An apptainer placement is read with its login node, its Slurm settings, how it submits, and the
// AMD GPUs it gives; its apptainer section may be left out for the defaults.
func TestAnApptainerPlacementIsRead(t *testing.T) {
	c, err := parse(apptainerPlacement)
	if err != nil {
		t.Fatal(err)
	}
	want := &cliv1.Placement{
		Kind: cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER, MaxJobs: 1,
		Remote:    &cliv1.RemoteSettings{Host: "host-a", RemoteWorkspaceRoot: "/shared/vibesys"},
		Slurm:     &cliv1.SlurmSettings{Partition: "part-a"},
		Apptainer: &cliv1.ApptainerSettings{SubmitCommand: []string{"/opt/site/bin/sbatch-wrapper"}, SubmitArguments: []string{"--account=acct-a"}},
		Resources: &cliv1.Resources{Rocm: &cliv1.GpuPool{Count: count(8)}, Mounts: map[string]string{"hf": "/shared/hf"}},
	}
	if got := c.GetPlacements()["mi210"]; !proto.Equal(got, want) {
		t.Errorf("placement\n%v\nwant\n%v", got, want)
	}
	text := strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]\nsubmit_command = [\"/opt/site/bin/sbatch-wrapper\"]\nsubmit_arguments = [\"--account=acct-a\"]\n", "", 1)
	c, err = parse(text)
	if err != nil || c.GetPlacements()["mi210"].GetApptainer() != nil {
		t.Errorf("without an apptainer section: %v, %v", c, err)
	}
	text = strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]",
		"[placements.mi210.apptainer]\nlocal_scratch_dir = \"/scratch/me\"\nconvert_timeout_seconds = 7200", 1)
	c, err = parse(text)
	a := c.GetPlacements()["mi210"].GetApptainer()
	if err != nil || a.GetLocalScratchDir() != "/scratch/me" || a.GetConvertTimeoutSeconds() != 7200 {
		t.Errorf("the conversion's settings: %v, %v", a, err)
	}
	text = strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nidle_release_seconds = 1800\nallocation_limit_seconds = 86400", 1)
	c, err = parse(text)
	if a := c.GetPlacements()["mi210"].GetApptainer(); err != nil || a.GetIdleReleaseSeconds() != 1800 || a.GetAllocationLimitSeconds() != 86400 {
		t.Errorf("the reuse of allocations: %v, %v", a, err)
	}
	text = strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.slurm]\nmax_time_seconds = 42600", 1)
	text = strings.Replace(text, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nidle_release_seconds = 900", 1)
	c, err = parse(text)
	if s := c.GetPlacements()["mi210"].GetSlurm(); err != nil || s.GetMaxTimeSeconds() != 42600 {
		t.Errorf("the cap on an allocation's time: %v, %v", s, err)
	}
	// Each job of an apptainer placement has allocations and staged directories of its own, so it
	// may run several at once.
	text = strings.Replace(apptainerPlacement, "kind = \"apptainer\"", "kind = \"apptainer\"\nmax_jobs = 3", 1)
	c, err = parse(text)
	if err != nil || c.GetPlacements()["mi210"].GetMaxJobs() != 3 {
		t.Errorf("three jobs at once: %v, %v", c, err)
	}
	text = strings.Replace(apptainerPlacement, "[placements.mi210.apptainer]", "[placements.mi210.apptainer]\nruntime = \"pyxis\"", 1)
	c, err = parse(text)
	if a := c.GetPlacements()["mi210"].GetApptainer(); err != nil || a.GetRuntime() != cliv1.ContainerRuntime_CONTAINER_RUNTIME_PYXIS {
		t.Errorf("the pyxis runtime: %v, %v", a, err)
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

// stop_grace_seconds is read with or without placements, and is unset when the file does not say.
func TestTheStopGraceIsReadAndUnsetWhenTheFileDoesNotSay(t *testing.T) {
	for name, text := range map[string]string{
		"no placements": "stop_grace_seconds = 12.5\n",
		"placements":    "stop_grace_seconds = 12.5\n" + placementsConfig,
	} {
		c, err := parse(text)
		if err != nil || c.StopGraceSeconds == nil || c.GetStopGraceSeconds() != 12.5 {
			t.Errorf("%s: stop_grace_seconds %v, %v; want 12.5", name, c.GetStopGraceSeconds(), err)
		}
	}
	for _, text := range []string{"", slurmConfig, placementsConfig} {
		if c, err := parse(text); err != nil || c.StopGraceSeconds != nil {
			t.Errorf("%q: %v, %v; want the grace unset", text, c.StopGraceSeconds, err)
		}
	}
}

func TestTheSchedulerDefaultsToFifo(t *testing.T) {
	for text, policy := range map[string]string{
		"":                                       "fifo",
		"[scheduler]\n":                          "fifo",
		"[scheduler]\npolicy = \"fair-share\"\n": "fair-share",
	} {
		c, err := parse(text)
		if err != nil || c.GetScheduler().GetPolicy() != policy {
			t.Errorf("%q: scheduler %v, %v; want policy %q", text, c.GetScheduler(), err, policy)
		}
	}
}

// A placement runs as many jobs at once as its max_jobs says. Unset, a placement of this machine
// with a pool of GPUs runs one per GPU, and every other placement one: a cluster's jobs are
// allocations of their own, an ssh placement's jobs share one staging scope on its host, and a
// placement whose GPUs are no pool gives every job all of them. An explicit value is refused where
// the placement runs one job at a time (ssh, slurm), with its key named, pool or not.
func TestMaxJobsDefaultsToThePoolsSizeOnAMachine(t *testing.T) {
	r := rand.New(rand.NewPCG(21, 22))
	for range 300 {
		size := 1 + r.IntN(64)
		table := []string{"gpu", "rocm"}[r.IntN(2)]
		for kind, section := range map[string]string{
			"local":     "",
			"ssh":       "[placements.x.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/w\"\n",
			"slurm":     "[placements.x.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/w\"\n[placements.x.slurm]\npartition = \"p\"\n",
			"apptainer": "[placements.x.remote]\nhost = \"host-a\"\nremote_workspace_root = \"/w\"\n[placements.x.slurm]\npartition = \"p\"\n",
		} {
			if kind == "apptainer" {
				table = "rocm"
			}
			pooled := fmt.Sprintf("[placements.x]\nkind = %q\n%s[placements.x.resources.%s]\ncount = %d\n", kind, section, table, size)
			want := uint32(1)
			if kind == "local" {
				want = uint32(size)
			}
			c, err := parse(pooled)
			if err != nil || c.GetPlacements()["x"].GetMaxJobs() != want {
				t.Fatalf("%s: max_jobs %d, %v; want %d", pooled, c.GetPlacements()["x"].GetMaxJobs(), err, want)
			}
			pool, err := operator.PoolOf("x", c.GetPlacements()["x"])
			if err != nil || pool.Size.Int() != size || pool.Key != "placements.x.resources."+table+".count" {
				t.Fatalf("%s: pool %+v, %v", pooled, pool, err)
			}
			if kind == "ssh" || kind == "slurm" {
				// These run one job at a time, with a pool or without: more is refused, naming the key.
				set := 2 + r.IntN(1000)
				if _, err := parse(strings.Replace(pooled, "kind = ", fmt.Sprintf("max_jobs = %d\nkind = ", set), 1)); err == nil || !strings.Contains(err.Error(), "placements.x.max_jobs") {
					t.Fatalf("%s with max_jobs %d: %v, want it refused naming placements.x.max_jobs", pooled, set, err)
				}
			}
			if kind == "slurm" || kind == "apptainer" || kind == "ssh" {
				continue
			}
			// The operator's own max_jobs wins, above the pool's size too, for jobs that need no GPU.
			set := uint32(1 + r.IntN(1024))
			c, err = parse(strings.Replace(pooled, "kind = ", fmt.Sprintf("max_jobs = %d\nkind = ", set), 1))
			if err != nil || c.GetPlacements()["x"].GetMaxJobs() != set {
				t.Fatalf("%s with max_jobs %d: %d, %v", pooled, set, c.GetPlacements()["x"].GetMaxJobs(), err)
			}
			// A table without a count is no pool: one job at a time.
			c, err = parse(strings.Replace(pooled, fmt.Sprintf("count = %d\n", size), "", 1))
			if err != nil || c.GetPlacements()["x"].GetMaxJobs() != 1 {
				t.Fatalf("%s without a count: %d, %v", pooled, c.GetPlacements()["x"].GetMaxJobs(), err)
			}
			if pool, err := operator.PoolOf("x", c.GetPlacements()["x"]); err != nil || pool.Size.IsSet() {
				t.Fatalf("a table without a count is the pool %+v, %v", pool, err)
			}
		}
	}
}

// Two placements of this machine may both give its GPUs whole, as long as neither makes them a pool.
func TestPlacementsOfOneMachineShareItsGPUsWithoutAPool(t *testing.T) {
	text := "[placements.a]\nkind = \"local\"\n[placements.a.resources.gpu]\n[placements.b]\nkind = \"local\"\nmax_jobs = 2\n[placements.b.resources.gpu]\n"
	if _, err := parse(text); err != nil {
		t.Errorf("two placements of the machine's GPUs: %v", err)
	}
}

// A placement of any name, kind and resources, written in the file, reads back as written, and a
// key put anywhere that the schema has no field for is refused, naming its whole path.
func TestAnyPlacementsReadAsWrittenAndAnUnknownKeyAnywhereIsNamed(t *testing.T) {
	r := rand.New(rand.NewPCG(11, 12))
	kinds := []cliv1.PlacementKind{cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, cliv1.PlacementKind_PLACEMENT_KIND_SSH, cliv1.PlacementKind_PLACEMENT_KIND_SLURM}
	for range 300 {
		want := &cliv1.OperatorConfig{Version: "0.2", Scheduler: &cliv1.Scheduler{Policy: proto.String("fifo")}, Placements: map[string]*cliv1.Placement{}}
		var text strings.Builder
		text.WriteString(v02)
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
			if r.IntN(3) == 0 {
				held, hours := uint32(1+r.IntN(64)), 1+r.IntN(48)
				p.Allocations = &cliv1.AllocationSettings{MaxHeld: held, MaxFor: durationpb.New(time.Duration(hours) * time.Hour)}
				fmt.Fprintf(&text, "[placements.%s.allocations]\nmax_held = %d\nmax_for = \"%dh\"\n", name, held, hours)
				tables = append(tables, "placements."+name+".allocations")
			}
			if r.IntN(2) == 0 {
				p.Resources = &cliv1.Resources{}
				if r.IntN(2) == 0 {
					// A cluster's GPUs are a pool; a machine's are given whole, since several
					// placements here share one machine, whose GPUs are one pool at most.
					p.Resources.Gpu = &cliv1.GpuPool{}
					fmt.Fprintf(&text, "[placements.%s.resources.gpu]\n", name)
					if p.Kind == cliv1.PlacementKind_PLACEMENT_KIND_SLURM {
						p.Resources.Gpu.Count = count(uint32(1 + r.IntN(64)))
						fmt.Fprintf(&text, "count = %d\n", p.Resources.Gpu.GetCount())
					}
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
	if err := os.WriteFile(file, []byte(v02+"[placements.local]\nkind = \"local\"\n[placements.local.resources.gpu]\ncount = 2\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if c, err := operator.Load(file, true); err != nil || c.GetPlacements()["local"].GetResources().GetGpu().GetCount() != 2 {
		t.Errorf("load: %v, %v", c, err)
	}
	// A file that is there and not of version 0.2 is refused, naming the file: it is never the
	// default.
	if err := os.WriteFile(file, []byte("[docker]\ngpus = \"all\"\n"), 0o600); err != nil {
		t.Fatal(err)
	}
	if _, err := operator.Load(file, false); !refused(err, file, "docker:", `"0.2"`) {
		t.Errorf("load of the shape before 0.2: %v", err)
	}
}

// More placements than ListPlacements can report are refused, naming the key; the most it can are read.
func TestMorePlacementsThanListPlacementsReportsAreRefused(t *testing.T) {
	config := func(n int) []byte {
		var b strings.Builder
		b.WriteString(v02)
		for i := range n {
			fmt.Fprintf(&b, "[placements.place-%d]\nkind = \"local\"\n", i)
		}
		return []byte(b.String())
	}
	got, err := operator.Parse(config(operator.MaxPlacements))
	if err != nil || len(got.GetPlacements()) != operator.MaxPlacements {
		t.Fatalf("%d placements: %v, %v", operator.MaxPlacements, len(got.GetPlacements()), err)
	}
	_, err = operator.Parse(config(operator.MaxPlacements + 1))
	var f *core.Failure
	if !errors.As(err, &f) || f.Kind != core.FailureInvalidInput || !strings.Contains(err.Error(), "placements:") {
		t.Fatalf("got %v, want invalid input naming placements", err)
	}
}

// A placement's allocations section is read as written, its max_for a duration as a flag takes one;
// a placement without one offers no standing allocations.
func TestAPlacementsAllocationsAreRead(t *testing.T) {
	text := strings.Replace(apptainerPlacement, "[placements.mi210.slurm]", "[placements.mi210.allocations]\nmax_held = 2\nmax_for = \"12h\"\n[placements.mi210.slurm]\nmax_time_seconds = 43200", 1) +
		"[placements.local]\nkind = \"local\"\n"
	c, err := parse(text)
	if err != nil {
		t.Fatal(err)
	}
	got := c.GetPlacements()["mi210"].GetAllocations()
	if got.GetMaxHeld() != 2 || got.GetMaxFor().AsDuration() != 12*time.Hour {
		t.Errorf("allocations = %v, want max_held 2 and max_for 12h", got)
	}
	if c.GetPlacements()["local"].GetAllocations() != nil {
		t.Errorf("a placement with no allocations section has %v", c.GetPlacements()["local"].GetAllocations())
	}
}

// Every operator config the documentation shows is read, so an example can never be one the CLI
// refuses: each of the CLI README's examples, one per kind of placement and one of several, and
// the contributor guide's.
func TestEveryOperatorConfigTheDocumentationShowsIsRead(t *testing.T) {
	for file, least := range map[string]int{"../../README.md": 6, "../../../docs/contributing/cli-task-environment.md": 1} {
		data, err := os.ReadFile(file)
		if err != nil {
			t.Fatal(err)
		}
		blocks := strings.Split(string(data), "```toml\n")[1:]
		var read int
		for _, block := range blocks {
			text, _, _ := strings.Cut(block, "```")
			if !strings.HasPrefix(text, v02) || !strings.Contains(text, "\n[placements.") {
				continue // a fragment of a file, such as one section, or a task manifest
			}
			c, err := operator.Parse([]byte(text))
			if err != nil {
				t.Errorf("%s: an example is refused: %v\n%s", file, err, text)
				continue
			}
			for name, p := range c.GetPlacements() {
				if pool, err := operator.PoolOf(name, p); err != nil || !pool.Size.IsSet() {
					t.Errorf("%s: placement %s of an example has no pool of GPUs: %v", file, name, err)
				}
			}
			read++
		}
		if read < least {
			t.Errorf("%s: %d whole operator configs, want at least %d", file, read, least)
		}
	}
}
