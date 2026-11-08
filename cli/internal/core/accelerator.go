package core

import (
	"strconv"
	"strings"
	"time"
)

// AcceleratorVendor is who makes an accelerator.
type AcceleratorVendor uint8

// The vendors of accelerators a placement can give. The zero value is no vendor.
const (
	VendorNVIDIA AcceleratorVendor = iota + 1
	VendorAMD
)

// AcceleratorModel is a device model the CLI names. ModelOther is a device of the vendor that has no
// name here; the device's own name says which.
type AcceleratorModel string

// The models the CLI names. This list is temporary until the shared accelerator identity registry
// replaces it.
const (
	ModelOther  AcceleratorModel = "other"
	ModelH100   AcceleratorModel = "h100"
	ModelB200   AcceleratorModel = "b200"
	ModelL4     AcceleratorModel = "l4"
	ModelA100   AcceleratorModel = "a100"
	ModelMI210  AcceleratorModel = "mi210"
	ModelMI300A AcceleratorModel = "mi300a"
	ModelMI300X AcceleratorModel = "mi300x"
	ModelMI325X AcceleratorModel = "mi325x"
)

// Accelerator is what a placement gives: Count devices of one model.
type Accelerator struct {
	Vendor AcceleratorVendor
	Model  AcceleratorModel
	Count  uint32
	// DeviceName is the device as it reported itself.
	DeviceName string
}

// MaxAccelerators is the most devices of one model an Accelerator names.
const MaxAccelerators = 4096

// modelNames maps the upper-case token of a device name to its model, by vendor.
var modelNames = map[AcceleratorVendor]map[string]AcceleratorModel{
	VendorNVIDIA: {"H100": ModelH100, "B200": ModelB200, "L4": ModelL4, "A100": ModelA100},
	VendorAMD:    {"MI210": ModelMI210, "MI300A": ModelMI300A, "MI300X": ModelMI300X, "MI325X": ModelMI325X},
}

// ParseAccelerator reads a device name and a count as a fingerprint or a gres states them. It fails
// with FailureInvalidInput for a count that is not 1 to MaxAccelerators, and for an empty or over-long name.
func ParseAccelerator(vendor AcceleratorVendor, name, count string) (Accelerator, error) {
	n, err := strconv.ParseUint(count, 10, 32)
	if err != nil || n == 0 || n > MaxAccelerators || name == "" || len(name) > 128 {
		return Accelerator{}, failf(FailureInvalidInput, "unreadable accelerator model %q or count %q", name, count)
	}
	model := ModelOther
	// Match whole tokens rather than substrings: L40 must not be reported as L4.
	for _, word := range strings.FieldsFunc(strings.ToUpper(name), func(r rune) bool { return !(r >= 'A' && r <= 'Z' || r >= '0' && r <= '9') }) {
		if m, ok := modelNames[vendor][word]; ok {
			model = m
			break
		}
	}
	return Accelerator{Vendor: vendor, Model: model, Count: uint32(n), DeviceName: name}, nil
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
	// Reachable is whether the placement answered; Reason says why not when it did not.
	Reachable bool
	Reason    string
}
