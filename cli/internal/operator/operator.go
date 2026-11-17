// Package operator reads the operator config: the settings of the machines that run vibesys-cli's
// jobs, which belong to whoever runs the CLI and never to a task. The schema is OperatorConfig in
// proto/vibesys/cli/v1/operator.proto, validated by its protovalidate rules: the file says
// version = "0.2", names placements (where a docker job's container runs: this machine, another one
// over ssh, or another one inside a Slurm allocation, or the compute nodes of a Slurm cluster under
// Apptainer), and what each gives a container, by resource kind, among them its GPUs, whose count
// makes them a pool that jobs share.
//
// This package keeps only the outer structure: it decodes the TOML into the message, refuses what
// the message has no field for and every key of the shape before version 0.2, naming the key,
// checks that each placement has the sections its kind uses and no other, and fills in what the
// file leaves to its default. What a kind's settings mean is the business of the package that
// implements the kind, which the binary's wiring hands its section to. No file is the default: one
// local placement with no resource.
package operator

import (
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"math"
	"os"
	"path"
	"slices"
	"strings"

	"buf.build/go/protovalidate"
	"github.com/BurntSushi/toml"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// EnvConfig names the variable that points at another operator config file.
const EnvConfig = "VIBESYS_CLI_CONFIG"

// Version is the version of the operator config's schema, the only one this build reads.
const Version = "0.2"

// DefaultPlacement is the name of the one placement of no file, and of a file that names none.
const DefaultPlacement = "default"

// readme is where a refusal of the file's version sends the operator.
const readme = `see "Operator config" in cli/README.md`

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
func Load(file string, named bool) (*cliv1.OperatorConfig, error) {
	data, err := os.ReadFile(file)
	switch {
	case err == nil:
		c, err := Parse(data)
		if err != nil {
			return nil, fmt.Errorf("the operator config %s: %w", file, err)
		}
		return c, nil
	case errors.Is(err, fs.ErrNotExist) && !named:
		return Default(), nil
	}
	return nil, invalid("reading the operator config: %v", err)
}

// Default is the config of no file: the one placement "default" of kind local, with no resource,
// running one job at a time, and the default policy.
func Default() *cliv1.OperatorConfig {
	return &cliv1.OperatorConfig{
		Version:    Version,
		Placements: map[string]*cliv1.Placement{DefaultPlacement: {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL, MaxJobs: 1}},
		Scheduler:  &cliv1.Scheduler{Policy: proto.String(DefaultPolicy)},
	}
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// Parse reads and checks an operator config, which must be of version 0.2. A file that names no
// placement is the placement "default" of kind local. In the result every placement's max_jobs is
// set: the file's, or for a placement with a GPU pool on the local or ssh kind the pool's size, or
// one. Its scheduler names a policy. Its stop_grace_seconds is the file's, or unset.
func Parse(data []byte) (*cliv1.OperatorConfig, error) {
	var raw map[string]any
	if _, err := toml.Decode(string(data), &raw); err != nil {
		return nil, invalid("%v", err)
	}
	if err := checkVersion(raw); err != nil {
		return nil, err
	}
	c := &cliv1.OperatorConfig{}
	if err := tomlproto.DecodeTable(raw, c, tomlproto.Options{}); err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(c); err != nil {
		return nil, invalid("%s", strings.TrimPrefix(strings.ReplaceAll(err.Error(), "\n", " "), "validation error: "))
	}
	if n := len(c.GetPlacements()); n > MaxPlacements {
		return nil, invalid("placements: %d placements, at most %d (the most ListPlacements reports)", n, MaxPlacements)
	}
	if len(c.GetPlacements()) == 0 {
		c.Placements = map[string]*cliv1.Placement{DefaultPlacement: {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL}}
	}
	if c.GetScheduler().GetPolicy() == "" {
		c.Scheduler = &cliv1.Scheduler{Policy: proto.String(DefaultPolicy)}
	}
	for _, name := range slices.Sorted(maps.Keys(c.GetPlacements())) {
		p := c.GetPlacements()[name]
		pool, err := PoolOf(name, p)
		if err != nil {
			return nil, err
		}
		if err := sections(p, namesOf(name), pool); err != nil {
			return nil, err
		}
		if p.GetMaxJobs() == 0 {
			p.MaxJobs = defaultMaxJobs(p.GetKind(), pool)
		}
	}
	if err := poolsOnce(c); err != nil {
		return nil, err
	}
	return c, nil
}

// legacyKeys are the keys of the shape before version 0.2, by their path in the file ("*" for any
// placement's name), with what version 0.2 writes instead. Each is refused by name, before the
// version, so that a file of the earlier shape is told what to change and not only that its version
// is missing.
var legacyKeys = []struct{ path, instead string }{
	{"docker", "name every placement under [placements.<name>]"},
	{"remote", "write it as [placements.<name>.remote]"},
	{"slurm", "write it as [placements.<name>.slurm]"},
	{"scheduler.max_jobs", "set max_jobs on each placement"},
	{"placements.*.resources.gpu.devices", "write count, the number of the machine's GPUs"},
	{"placements.*.slurm.gres", "write the GPUs of each node as resources.gpu.count or resources.rocm.count, and their type as gres_type there"},
	{"placements.*.slurm.shared_gres", "an allocation asks for the GPUs a job asks for"},
	{"placements.*.slurm.exclusive", "a measure's allocation always holds its whole node"},
}

// checkVersion refuses a key of the shape before version 0.2, naming it, and then a file that does
// not say version = "0.2".
func checkVersion(raw map[string]any) error {
	for _, k := range legacyKeys {
		if found, ok := lookup(raw, strings.Split(k.path, "."), ""); ok {
			return invalid("%s: a key of the operator config before version %s, which this build does not read; %s, and say version = %q at the top of the file (%s)",
				found, Version, k.instead, Version, readme)
		}
	}
	v, ok := raw["version"]
	switch {
	case !ok:
		return invalid("version: missing; the operator config says version = %q at the top of the file (%s)", Version, readme)
	case v != Version:
		return invalid("version = %#v: this build reads version %q of the operator config only (%s)", v, Version, readme)
	}
	return nil
}

// lookup finds the key at the path in the table, "*" matching any key, and returns its full name.
func lookup(table map[string]any, keys []string, prefix string) (string, bool) {
	names := []string{keys[0]}
	if keys[0] == "*" {
		names = slices.Sorted(maps.Keys(table))
	}
	for _, name := range names {
		v, ok := table[name]
		if !ok {
			continue
		}
		full := prefix + name
		if len(keys) == 1 {
			return full, true
		}
		if sub, ok := v.(map[string]any); ok {
			if found, ok := lookup(sub, keys[1:], full+"."); ok {
				return found, true
			}
		}
	}
	return "", false
}

// MaxPlacements is the most placements a config names: the bound of ListPlacementsResponse.
const MaxPlacements = 64

// DefaultPolicy is the scheduling policy of a config that names none: the oldest job first.
const DefaultPolicy = "fifo"

// Pool is a placement's pool of GPUs, as its file declares it: the vendor's table with a count.
// The zero value, with no Size, is a placement without a pool, whose GPUs (if it gives any) every
// job gets whole.
type Pool struct {
	Vendor core.AcceleratorVendor
	// Size is the table's count: the GPUs 0 to Size-1 of the machine, or of each node of a cluster.
	Size core.GPUCount
	// GresType is the GPU type an allocation of a Slurm cluster names, empty for none.
	GresType string
	// Key is where the count is written, such as placements.box.resources.gpu.count, for the
	// refusals that name it.
	Key string
}

// PoolOf is the pool of the placement of the name, or the zero Pool when it declares none. It fails
// naming the key for a count out of range, which Parse refuses before.
func PoolOf(name string, p *cliv1.Placement) (Pool, error) {
	table, vendor, kind := p.GetResources().GetGpu(), core.VendorNVIDIA, "gpu"
	if table == nil {
		table, vendor, kind = p.GetResources().GetRocm(), core.VendorAMD, "rocm"
	}
	if table == nil || table.Count == nil {
		return Pool{}, nil
	}
	key := "placements." + name + ".resources." + kind + ".count"
	size, err := core.ParseGPUCount(table.GetCount(), key)
	if err != nil {
		return Pool{}, err
	}
	return Pool{Vendor: vendor, Size: size, GresType: table.GetGresType(), Key: key}, nil
}

// defaultMaxJobs is how many jobs a placement runs at once when the file does not say: one per GPU
// of its pool on this machine, where the CLI hands each job its own GPUs, and one otherwise: each
// job of a cluster is an allocation of its own, and the jobs of an ssh placement share one staging
// scope on its host, so it runs one at a time whatever its pool.
func defaultMaxJobs(kind cliv1.PlacementKind, pool Pool) uint32 {
	if pool.Size.IsSet() && kind == cliv1.PlacementKind_PLACEMENT_KIND_LOCAL {
		return uint32(pool.Size.Int())
	}
	return 1
}

// poolsOnce refuses two placements that declare the GPUs of one vendor on one machine when either
// makes them a pool: a machine's GPUs are one pool, and two would hand one GPU to two jobs. This
// machine is every local placement's, and another machine is an ssh placement's host. A cluster's
// placements are kept apart by the cluster, which gives each allocation GPUs of its own.
func poolsOnce(c *cliv1.OperatorConfig) error {
	type gpus struct {
		machine string
		vendor  core.AcceleratorVendor
	}
	first := map[gpus]string{}
	pooled := map[gpus]bool{}
	for _, name := range slices.Sorted(maps.Keys(c.GetPlacements())) {
		p := c.GetPlacements()[name]
		var machine string
		switch p.GetKind() {
		case cliv1.PlacementKind_PLACEMENT_KIND_LOCAL:
			machine = "this machine"
		case cliv1.PlacementKind_PLACEMENT_KIND_SSH:
			machine = p.GetRemote().GetHost()
		default:
			continue
		}
		for _, t := range gpuTablesOf(p) {
			vendor, table := t.vendor, t.table
			if table == nil {
				continue
			}
			key := gpus{machine, vendor}
			other, seen := first[key]
			pool := table.Count != nil
			if seen && (pool || pooled[key]) {
				return invalid("placements.%s.resources.%s: placement %q declares the same GPUs of %s, and one of them makes them a pool with count; a machine's GPUs are declared once",
					name, tableName(vendor), other, machine)
			}
			if !seen {
				first[key] = name
			}
			pooled[key] = pooled[key] || pool
		}
	}
	return nil
}

// gpuTable is a placement's table of one vendor's GPUs, nil when the file has none.
type gpuTable struct {
	vendor core.AcceleratorVendor
	table  *cliv1.GpuPool
}

// gpuTablesOf are the placement's GPU tables of every vendor, in a fixed order.
func gpuTablesOf(p *cliv1.Placement) []gpuTable {
	return []gpuTable{{core.VendorNVIDIA, p.GetResources().GetGpu()}, {core.VendorAMD, p.GetResources().GetRocm()}}
}

// tableName is the resource table of a vendor's GPUs.
func tableName(v core.AcceleratorVendor) string {
	if v == core.VendorAMD {
		return "rocm"
	}
	return "gpu"
}

// sectionNames are how a placement's settings are spelled in the file, for its errors.
type sectionNames struct{ placement, kind, remote, slurm, apptainer, maxJobs, allocations, resources string }

func namesOf(name string) sectionNames {
	p := "placements." + name
	return sectionNames{
		placement: name, kind: p + ".kind", remote: "[" + p + ".remote]", slurm: "[" + p + ".slurm]",
		apptainer: "[" + p + ".apptainer]", maxJobs: p + ".max_jobs", allocations: "[" + p + ".allocations]",
		resources: p + ".resources",
	}
}

// sections refuses a placement without a section its kind needs, or with one its kind does not
// use: a setting quietly left out would run jobs somewhere the operator did not mean.
func sections(p *cliv1.Placement, n sectionNames, pool Pool) error {
	if err := gpuTables(p, n, pool); err != nil {
		return err
	}
	kind := kindName(p.GetKind())
	remote := p.GetKind() != cliv1.PlacementKind_PLACEMENT_KIND_LOCAL
	apptainer := p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER
	slurm := p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_SLURM || apptainer
	pyxis := p.GetApptainer().GetRuntime() == cliv1.ContainerRuntime_CONTAINER_RUNTIME_PYXIS
	switch {
	case remote && p.GetRemote() == nil:
		return invalid("%s %q needs a %s section with the host", n.kind, kind, n.remote)
	case !remote && p.GetRemote() != nil:
		return invalid("%s is set, and %s %q does not use it", n.remote, n.kind, kind)
	case slurm && p.GetSlurm() == nil:
		return invalid("%s %q needs a %s section", n.kind, kind, n.slurm)
	case !slurm && p.GetSlurm() != nil:
		return invalid("%s is set, and %s %q does not use it", n.slurm, n.kind, kind)
	case !apptainer && p.GetApptainer() != nil:
		return invalid("%s is set, and %s %q does not use it", n.apptainer, n.kind, kind)
	case apptainer && len(p.GetSlurm().GetSrunArguments()) > 0:
		// The apptainer kind's allocations are batch jobs of the submit command, whose own options
		// are submit_arguments.
		return invalid("%s srun_arguments: %s %q submits batch jobs; set the options in %s submit_arguments", n.slurm, n.kind, kind, n.apptainer)
	case apptainer && p.GetResources().GetGpu() != nil:
		return invalid("%s %q gives AMD GPUs only, with a rocm resource; it has no gpu resource", n.kind, kind)
	case apptainer && p.GetResources().GetKubernetes() != nil:
		return invalid("%s %q gives no kubernetes resource", n.kind, kind)
	case p.GetApptainer().GetAllocationLimitSeconds() > 0 && p.GetApptainer().GetIdleReleaseSeconds() == 0:
		return invalid("%s allocation_limit_seconds: it limits reused allocations, and idle_release_seconds is unset, so none is reused", n.apptainer)
	case !apptainer && p.GetSlurm().GetMaxTimeSeconds() > 0:
		// The slurm kind's srun jobs are not capped by it: a cap that is quietly ignored would let a
		// job ask for more than the site grants.
		return invalid("%s max_time_seconds: only %s \"apptainer\" caps its allocations by it", n.slurm, n.kind)
	case math.Mod(p.GetSlurm().GetMaxTimeSeconds(), 60) != 0:
		return invalid("%s max_time_seconds = %v: Slurm counts a time limit in whole minutes; give a multiple of 60", n.slurm, p.GetSlurm().GetMaxTimeSeconds())
	case p.GetSlurm().GetMaxTimeSeconds() > 0 && p.GetApptainer().GetAllocationLimitSeconds() > p.GetSlurm().GetMaxTimeSeconds():
		return invalid("%s allocation_limit_seconds = %v is longer than %s max_time_seconds = %v, the longest allocation the cluster grants; lower it, or leave it unset for max_time_seconds",
			n.apptainer, p.GetApptainer().GetAllocationLimitSeconds(), n.slurm, p.GetSlurm().GetMaxTimeSeconds())
	case p.GetSlurm().GetMaxTimeSeconds() > 0 && p.GetAllocations().GetMaxFor().AsDuration().Seconds() > p.GetSlurm().GetMaxTimeSeconds():
		// A standing allocation asked for longer than the cluster grants would be refused by the
		// cluster after it was accepted here.
		return invalid("%s max_for = %s is longer than %s max_time_seconds = %v, the longest allocation the cluster grants",
			n.allocations, p.GetAllocations().GetMaxFor().AsDuration(), n.slurm, p.GetSlurm().GetMaxTimeSeconds())
	case pyxis && p.GetApptainer().GetProgram() != "":
		// The pyxis runtime runs no apptainer program on the nodes, and converts nothing on this
		// machine: a setting of the other runtime would be quietly ignored.
		return invalid("%s program: runtime \"pyxis\" runs no apptainer program", n.apptainer)
	case pyxis && p.GetApptainer().GetLocalScratchDir() != "":
		return invalid("%s local_scratch_dir: runtime \"pyxis\" converts the images on the login node, not on this machine", n.apptainer)
	case p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_SLURM && p.GetMaxJobs() > 1:
		// A slurm placement's jobs share one Docker daemon and one image builder on the remote
		// machine, and it stages each job's directories and sweeps its containers and allocations
		// as the one job of its scope.
		return invalid("%s: %s %q runs one job at a time; more than 1 needs %s \"local\" or \"apptainer\"", n.maxJobs, n.kind, kind, n.kind)
	case p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_SSH && p.GetMaxJobs() > 1:
		// An ssh placement stages each job's directories and sweeps its containers as the one job of
		// its scope, so a second job would sweep the first's. Its GPU pool still confines the one job
		// to the GPUs it asks for.
		return invalid("%s: %s %q runs one job at a time; more than 1 needs %s \"local\" or \"apptainer\"", n.maxJobs, n.kind, kind, n.kind)
	}
	return nil
}

// gpuTables refuses a GPU table its placement cannot use: both vendors on one placement, a
// runtime on AMD GPUs, a GRES type off a cluster, and a cluster's GPUs without a count, which its
// allocations ask for.
func gpuTables(p *cliv1.Placement, n sectionNames, pool Pool) error {
	gpu, rocm := p.GetResources().GetGpu(), p.GetResources().GetRocm()
	cluster := p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_SLURM || p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER
	switch {
	case gpu != nil && rocm != nil:
		return invalid("%s.rocm: [%s.gpu] is set too; a placement gives the GPUs of one vendor", n.resources, n.resources)
	case rocm.GetRuntime() != "":
		return invalid("%s.rocm.runtime: only the GPUs of [%s.gpu] are passed by a container runtime", n.resources, n.resources)
	}
	for _, t := range gpuTablesOf(p) {
		key, table := n.resources+"."+tableName(t.vendor), t.table
		switch {
		case table == nil:
		case table.GetGresType() != "" && !cluster:
			return invalid("%s.gres_type: only a placement of kind \"slurm\" or \"apptainer\" asks a cluster for GPUs by type", key)
		case cluster && !pool.Size.IsSet():
			return invalid("%s.count: %s %q asks the cluster for GPUs by count; set count, the GPUs of each node", key, n.kind, kindName(p.GetKind()))
		}
	}
	return nil
}

// kindName is how the file spells a placement kind.
func kindName(k cliv1.PlacementKind) string {
	return strings.ToLower(strings.TrimPrefix(k.String(), "PLACEMENT_KIND_"))
}
