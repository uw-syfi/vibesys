// Package operator reads the operator config: the settings of the machines that run vibesys-cli's
// jobs, which belong to whoever runs the CLI and never to a task. The schema is OperatorConfig in
// proto/vibesys/cli/v1/operator.proto, validated by its protovalidate rules: the file names
// placements (where a docker job's container runs: this machine, another one over ssh, or another
// one inside a Slurm allocation, or the compute nodes of a Slurm cluster under Apptainer), and what
// each gives a container, by resource kind.
//
// This package keeps only the outer structure: it decodes the TOML into the message, refuses what
// the message has no field for, naming the key, reads a file of the earlier shape as the one
// placement "default", and checks that each placement has the sections its kind uses and no
// other. What a kind's settings mean is the business of the package that implements the kind,
// which the binary's wiring hands its section to. No file is the default: one local placement with
// no resource.
package operator

import (
	"errors"
	"fmt"
	"io/fs"
	"maps"
	"os"
	"path"
	"slices"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/protobuf/proto"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/tomlproto"
)

// EnvConfig names the variable that points at another operator config file.
const EnvConfig = "VIBESYS_CLI_CONFIG"

// DefaultPlacement is the name of the one placement of a file of the earlier shape, and of no
// file.
const DefaultPlacement = "default"

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
		return Parse(nil)
	}
	return nil, invalid("reading the operator config: %v", err)
}

func invalid(format string, args ...any) error {
	return &core.Failure{Kind: core.FailureInvalidInput, Message: fmt.Sprintf(format, args...)}
}

// Parse reads and checks an operator config. The result names its placements only: one of the
// earlier shape is the placement "default", and every placement's max_jobs is at least one. Its
// scheduler names a policy, and no max_jobs: the earlier shape's scheduler.max_jobs is the max_jobs
// of the placement "default". Its stop_grace_seconds is the file's, or unset.
func Parse(data []byte) (*cliv1.OperatorConfig, error) {
	c := &cliv1.OperatorConfig{}
	if err := tomlproto.Decode(data, c, tomlproto.Options{}); err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(c); err != nil {
		return nil, invalid("%s", strings.TrimPrefix(strings.ReplaceAll(err.Error(), "\n", " "), "validation error: "))
	}
	sched := c.GetScheduler()
	if sched == nil {
		sched = &cliv1.Scheduler{}
	}
	grace := c.StopGraceSeconds
	legacy := c.GetDocker() != nil || c.GetRemote() != nil || c.GetSlurm() != nil
	named := len(c.GetPlacements()) > 0
	switch {
	case legacy && named:
		return nil, invalid("the config has [placements.*] and the earlier [docker], [remote] or [slurm] sections; name every placement under [placements.<name>]")
	case named && sched.MaxJobs != nil:
		return nil, invalid("scheduler.max_jobs: the config has [placements.*]; set max_jobs on each placement instead")
	case legacy:
		p, err := fromLegacy(c)
		if err != nil {
			return nil, err
		}
		c = &cliv1.OperatorConfig{Placements: map[string]*cliv1.Placement{DefaultPlacement: p}, StopGraceSeconds: grace}
	case !named:
		c.Placements = map[string]*cliv1.Placement{DefaultPlacement: {Kind: cliv1.PlacementKind_PLACEMENT_KIND_LOCAL}}
	}
	if sched.MaxJobs != nil {
		c.GetPlacements()[DefaultPlacement].MaxJobs = sched.GetMaxJobs()
	}
	c.Scheduler = &cliv1.Scheduler{Policy: proto.String(DefaultPolicy)}
	if sched.Policy != nil {
		c.Scheduler.Policy = sched.Policy
	}
	for _, name := range slices.Sorted(maps.Keys(c.GetPlacements())) {
		p := c.GetPlacements()[name]
		if p.GetMaxJobs() == 0 {
			p.MaxJobs = 1
		}
		names := sectionNames{
			kind: "placements." + name + ".kind", remote: "[placements." + name + ".remote]", slurm: "[placements." + name + ".slurm]",
			apptainer: "[placements." + name + ".apptainer]", maxJobs: "placements." + name + ".max_jobs",
		}
		if legacy || (!named && sched.MaxJobs != nil) {
			names = legacyNames
		}
		if err := sections(p, names); err != nil {
			return nil, err
		}
	}
	return c, nil
}

// DefaultPolicy is the scheduling policy of a config that names none: the oldest job first.
const DefaultPolicy = "fifo"

// fromLegacy is the placement a file of the earlier shape describes.
func fromLegacy(c *cliv1.OperatorConfig) (*cliv1.Placement, error) {
	d := c.GetDocker()
	p := &cliv1.Placement{Kind: d.GetPlacement(), Remote: c.GetRemote(), Slurm: c.GetSlurm(), MaxJobs: 1}
	if p.Kind == cliv1.PlacementKind_PLACEMENT_KIND_UNSPECIFIED {
		p.Kind = cliv1.PlacementKind_PLACEMENT_KIND_LOCAL
	}
	if d.GetRuntime() != "" && d.GetGpus() == "" {
		return nil, invalid("docker.runtime: %q passes GPUs only when docker.gpus names them", d.GetRuntime())
	}
	if d.GetGpus() != "" || len(d.GetMounts()) > 0 {
		p.Resources = &cliv1.Resources{Mounts: d.GetMounts()}
		if d.GetGpus() != "" {
			p.Resources.Gpu = &cliv1.GpuResource{Devices: d.GetGpus(), Runtime: d.GetRuntime()}
		}
	}
	return p, nil
}

// sectionNames are how a placement's settings are spelled in the file, for its errors.
type sectionNames struct{ kind, remote, slurm, apptainer, maxJobs string }

// legacyNames are how the earlier shape spells its one placement's settings. It has no apptainer
// section, which its one placement takes the defaults of.
var legacyNames = sectionNames{kind: "docker.placement", remote: "[remote]", slurm: "[slurm]", apptainer: "[apptainer]", maxJobs: "scheduler.max_jobs"}

// sections refuses a placement without a section its kind needs, or with one its kind does not
// use: a setting quietly left out would run jobs somewhere the operator did not mean.
func sections(p *cliv1.Placement, n sectionNames) error {
	kind := kindName(p.GetKind())
	remote := p.GetKind() != cliv1.PlacementKind_PLACEMENT_KIND_LOCAL
	apptainer := p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_APPTAINER
	slurm := p.GetKind() == cliv1.PlacementKind_PLACEMENT_KIND_SLURM || apptainer
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
	case remote && p.GetMaxJobs() > 1:
		// A remote placement stages each job's directories and sweeps its Slurm allocations as the
		// one job of its scope, so a second one beside it would be swept away.
		return invalid("%s: %s %q runs one job at a time; more than 1 needs %s \"local\"", n.maxJobs, n.kind, kind, n.kind)
	}
	return nil
}

// kindName is how the file spells a placement kind.
func kindName(k cliv1.PlacementKind) string {
	return strings.ToLower(strings.TrimPrefix(k.String(), "PLACEMENT_KIND_"))
}
