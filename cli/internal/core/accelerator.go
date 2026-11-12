package core

import (
	"slices"
	"strconv"
	"strings"
	"time"
)

// AcceleratorVendor is who makes an accelerator.
type AcceleratorVendor uint8

// The vendors of accelerators. The zero value is no vendor. VendorCPU is the vendor of the registry's
// "CPU" identity, which no placement offers as an accelerator.
const (
	VendorNVIDIA AcceleratorVendor = iota + 1
	VendorAMD
	VendorAWS
	VendorApple
	VendorCPU
)

// AcceleratorModel is a device model the CLI names. The empty value is any model of a vendor (the
// registry's "NVIDIA GPU"). ModelOther is a device of the vendor that has no name in the registry:
// the device's own name, kept in Accelerator.DeviceName, says which.
type AcceleratorModel string

// The models of the registry, one per row of it.
const (
	ModelOther        AcceleratorModel = "other"
	ModelH100         AcceleratorModel = "h100"
	ModelB200         AcceleratorModel = "b200"
	ModelL4           AcceleratorModel = "l4"
	ModelA100         AcceleratorModel = "a100"
	ModelMI210        AcceleratorModel = "mi210"
	ModelMI250        AcceleratorModel = "mi250"
	ModelMI300A       AcceleratorModel = "mi300a"
	ModelMI300X       AcceleratorModel = "mi300x"
	ModelMI325X       AcceleratorModel = "mi325x"
	ModelTrainium2    AcceleratorModel = "trainium2"
	ModelAppleSilicon AcceleratorModel = "apple_silicon"
)

// Accelerator is what a placement gives: Count devices of one model.
type Accelerator struct {
	Vendor AcceleratorVendor
	// Model is ModelOther, with DeviceName holding the raw text, for a device the registry does not
	// name: an unknown device is never read as a near match.
	Model AcceleratorModel
	Count uint32
	// DeviceName is the device as it reported itself.
	DeviceName string
}

// MaxAccelerators is the most devices of one model an Accelerator names.
const MaxAccelerators = 4096

// AcceleratorIdentity is one row of the accelerator registry: what a display name of a device names,
// and the probe aliases that name the same device on a machine.
type AcceleratorIdentity struct {
	Vendor AcceleratorVendor
	// Model is empty for a row that stands for any device of the vendor ("NVIDIA GPU") or for a CPU.
	Model AcceleratorModel
	// Display is the name a task authors, matched exactly.
	Display string
	// Aliases are the whole tokens, lower case, by which a probed device name or a gres names the
	// device: "h100" in "NVIDIA H100 80GB HBM3" and in "gpu:h100:8". A row with no model has none, and
	// so has a model no machine is probed for yet.
	Aliases []string
}

// accelerators is the registry: the one table of accelerator names. A task's display name and a
// machine's probe aliases both resolve through it, so the two cannot drift apart. It is closed: a
// new accelerator is a new row here, a new value of the contract's AcceleratorModel and a row of
// the contract mapping, together.
var accelerators = []AcceleratorIdentity{
	{VendorCPU, "", "CPU", nil},
	{VendorAMD, ModelMI210, "AMD MI210", []string{"mi210"}},
	// ROCm names the device "AMD Instinct MI250X / MI250"; its "MI250" token names it.
	{VendorAMD, ModelMI250, "AMD MI250", []string{"mi250"}},
	{VendorAMD, ModelMI300A, "AMD MI300A", []string{"mi300a"}},
	{VendorAMD, ModelMI300X, "AMD MI300X", []string{"mi300x"}},
	{VendorAMD, ModelMI325X, "AMD MI325X", []string{"mi325x"}},
	{VendorNVIDIA, "", "NVIDIA GPU", nil},
	{VendorNVIDIA, ModelH100, "NVIDIA H100", []string{"h100"}},
	{VendorNVIDIA, ModelB200, "NVIDIA B200", []string{"b200"}},
	{VendorNVIDIA, ModelL4, "NVIDIA L4", []string{"l4"}},
	{VendorNVIDIA, ModelA100, "NVIDIA A100", []string{"a100"}},
	{VendorAWS, ModelTrainium2, "AWS Trainium2", nil},
	{VendorApple, ModelAppleSilicon, "Apple Silicon", nil},
}

// Accelerators returns the registry's rows, in a fixed order.
func Accelerators() []AcceleratorIdentity {
	out := make([]AcceleratorIdentity, len(accelerators))
	for i, a := range accelerators {
		a.Aliases = slices.Clone(a.Aliases)
		out[i] = a
	}
	return out
}

// LookupAccelerator returns the row a display name names, and false for a name the registry does not
// have.
func LookupAccelerator(display string) (AcceleratorIdentity, bool) {
	for _, a := range Accelerators() {
		if a.Display == display {
			return a, true
		}
	}
	return AcceleratorIdentity{}, false
}

// ResolveAccelerator names the model of a vendor that a probed device name or a gres model token
// states, and returns ModelOther when the registry has no device of the vendor by it. The text is
// split into tokens at every character that is not a letter or digit and each token is matched whole
// against the vendor's aliases, in any case: "L40" is not "L4", and a device of another vendor is
// not read as this one. Text that names two models is no model.
func ResolveAccelerator(vendor AcceleratorVendor, text string) AcceleratorModel {
	found := ModelOther
	for _, word := range strings.FieldsFunc(strings.ToLower(text), func(r rune) bool { return !(r >= 'a' && r <= 'z' || r >= '0' && r <= '9') }) {
		for _, a := range accelerators {
			if a.Vendor != vendor || !slices.Contains(a.Aliases, word) {
				continue
			}
			if found != ModelOther && found != a.Model {
				return ModelOther
			}
			found = a.Model
		}
	}
	return found
}

// ParseAccelerator reads a device name and a count as a fingerprint or a gres states them, through
// the registry. A name the registry does not know gives ModelOther and the raw name in DeviceName.
// It fails with FailureInvalidInput for a count that is not 1 to MaxAccelerators, and for an empty
// or over-long name.
func ParseAccelerator(vendor AcceleratorVendor, name, count string) (Accelerator, error) {
	n, err := strconv.ParseUint(count, 10, 32)
	if err != nil || n == 0 || n > MaxAccelerators || name == "" || len(name) > 128 {
		return Accelerator{}, failf(FailureInvalidInput, "unreadable accelerator model %q or count %q", name, count)
	}
	return Accelerator{Vendor: vendor, Model: ResolveAccelerator(vendor, name), Count: uint32(n), DeviceName: name}, nil
}

// OfferSource says how the daemon learned what a placement gives.
type OfferSource string

// The sources of offers.
const (
	OfferProbed   OfferSource = "probed"
	OfferDeclared OfferSource = "declared"
)

// OffersUnknownReason says why a placement's hardware could not be read. The empty reason is
// "it was read".
type OffersUnknownReason string

// The reasons a placement's hardware is unknown.
const (
	OffersKnown        OffersUnknownReason = ""
	OffersUnreadable   OffersUnknownReason = "unreadable"
	OffersTimedOut     OffersUnknownReason = "timed_out"
	OffersNotProbeable OffersUnknownReason = "not_probeable"
)

// Reach is whether the daemon can reach a placement, in three states. The zero value is no state.
type Reach uint8

// The states of reach. A placement that was never asked is not unreachable: its declared offers
// still fit tasks.
const (
	ReachReachable Reach = iota + 1
	ReachUnreachable
	ReachNotProbed
)

// PlacementKind is where a placement's engine runs containers.
type PlacementKind uint8

// The kinds of placement. The zero value is no kind.
const (
	PlacementLocal PlacementKind = iota + 1
	PlacementSSH
	PlacementSlurm
	PlacementApptainer
)

// PlacementReading is one placement as the daemon last read it: its identity and the hardware it
// gives.
type PlacementReading struct {
	Name    string
	Kind    PlacementKind
	MaxJobs uint32
	// Unknown is why Offers could not be read, or OffersKnown.
	Unknown OffersUnknownReason
	// Detail names the placement and says what went wrong, when Unknown is not OffersKnown.
	Detail string
	// Offers and Source are set when the offers are known. No offers means no accelerator.
	Offers []Accelerator
	Source OfferSource
	// ProbedAt is when the offers were probed; zero for declared offers.
	ProbedAt time.Time
	// Reach is whether the daemon reached the placement when last asked. Reason says why not,
	// exactly when Reach is ReachUnreachable.
	Reach  Reach
	Reason string
}
