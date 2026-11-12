package fit_test

import (
	"math/rand"
	"slices"
	"testing"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/fit"
	"github.com/uw-syfi/vibesys/orchestrator/internal/wire"
)

const cases = 4000

// pairs are the legal vendor and model pairs of the contract; an unnamed model is legal for a need.
var pairs = []struct {
	vendor cli.AcceleratorVendor
	model  cli.AcceleratorModel
}{
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorModel_ACCELERATOR_MODEL_MI210},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorModel_ACCELERATOR_MODEL_MI250},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300A},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorModel_ACCELERATOR_MODEL_MI300X},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorModel_ACCELERATOR_MODEL_MI325X},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorModel_ACCELERATOR_MODEL_H100},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorModel_ACCELERATOR_MODEL_B200},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorModel_ACCELERATOR_MODEL_L4},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA, cli.AcceleratorModel_ACCELERATOR_MODEL_A100},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_AWS, cli.AcceleratorModel_ACCELERATOR_MODEL_TRAINIUM2},
	{cli.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE, cli.AcceleratorModel_ACCELERATOR_MODEL_APPLE_SILICON},
}

var accelerators = []cli.AcceleratorVendor{
	cli.AcceleratorVendor_ACCELERATOR_VENDOR_AMD, cli.AcceleratorVendor_ACCELERATOR_VENDOR_NVIDIA,
	cli.AcceleratorVendor_ACCELERATOR_VENDOR_AWS, cli.AcceleratorVendor_ACCELERATOR_VENDOR_APPLE,
}

func genOffer(r *rand.Rand) *pb.EvalOffer {
	if r.Intn(5) == 0 {
		return &pb.EvalOffer{Vendor: accelerators[r.Intn(len(accelerators))], Model: cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER, Count: uint32(1 + r.Intn(8)), DeviceName: "unknown device"}
	}
	p := pairs[r.Intn(len(pairs))]
	return &pb.EvalOffer{Vendor: p.vendor, Model: p.model, Count: uint32(1 + r.Intn(8))}
}

func genNeed(r *rand.Rand) *cli.AcceleratorNeed {
	switch r.Intn(8) {
	case 0:
		return nil
	case 1:
		return &cli.AcceleratorNeed{Vendor: cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU, Count: uint32(1 + r.Intn(8))}
	case 2, 3:
		return &cli.AcceleratorNeed{Vendor: accelerators[r.Intn(len(accelerators))], Count: uint32(1 + r.Intn(8))}
	default:
		p := pairs[r.Intn(len(pairs))]
		return &cli.AcceleratorNeed{Vendor: p.vendor, Model: p.model, Count: uint32(1 + r.Intn(8))}
	}
}

func known() *pb.EvalPlace_Known {
	return &pb.EvalPlace_Known{Known: &pb.OffersKnown{Source: pb.OffersSource_OFFERS_SOURCE_PROBED}}
}

func unreadable() *pb.EvalPlace_Unreadable {
	return &pb.EvalPlace_Unreadable{Unreadable: &pb.OffersUnreadable{Reason: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREACHABLE}}
}

func genPlace(r *rand.Rand, name string) *pb.EvalPlace {
	p := &pb.EvalPlace{Name: name, Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH}
	if r.Intn(4) == 0 {
		p.State = unreadable()
		// An unreadable place may still carry stale offers; they must not count.
		for range r.Intn(3) {
			p.Offers = append(p.Offers, genOffer(r))
		}
		return p
	}
	p.State = known()
	for range r.Intn(4) {
		p.Offers = append(p.Offers, genOffer(r))
	}
	return p
}

func genPlaces(r *rand.Rand) []*pb.EvalPlace {
	places := make([]*pb.EvalPlace, r.Intn(6))
	for i := range places {
		places[i] = genPlace(r, string(rune('a'+i)))
	}
	return places
}

func forAll(t *testing.T, property func(r *rand.Rand)) {
	t.Helper()
	for seed := range cases {
		property(rand.New(rand.NewSource(int64(seed))))
		if t.Failed() {
			t.Fatalf("seed %d", seed)
		}
	}
}

func TestAnUnreadablePlaceFitsNothing(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		place := genPlace(r, "p")
		place.State = unreadable()
		if need := genNeed(r); fit.Fits(need, place) {
			t.Errorf("unreadable place %v fits %v", place, need)
		}
	})
	// Even a CPU need and a task with no hardware.
	for _, need := range []*cli.AcceleratorNeed{nil, {Vendor: cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU, Count: 1}} {
		if fit.Fits(need, &pb.EvalPlace{Name: "p", State: unreadable()}) {
			t.Errorf("an unreadable place fits the CPU need %v", need)
		}
	}
	// A place with no state at all is unknown, and is not read as known.
	if fit.Fits(nil, &pb.EvalPlace{Name: "p"}) {
		t.Error("a place with no state fits")
	}
}

func TestACpuNeedFitsEveryKnownPlaceWhateverItOffers(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		place := genPlace(r, "p")
		cpu := &cli.AcceleratorNeed{Vendor: cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU, Count: uint32(1 + r.Intn(8))}
		for _, need := range []*cli.AcceleratorNeed{nil, cpu} {
			if got, want := fit.Fits(need, place), place.GetKnown() != nil; got != want {
				t.Errorf("need %v, place %v: %v, want %v", need, place, got, want)
			}
		}
	})
}

func TestAnOtherModelNeverFits(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		need := genNeed(r)
		place := genPlace(r, "p")
		base := fit.Fits(need, place)
		// Adding devices the registry does not know changes nothing.
		extra := genPlace(r, "p")
		extra.State = place.State
		extra.Offers = slices.Clone(place.Offers)
		for range 1 + r.Intn(3) {
			other := genOffer(r)
			other.Model = cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER
			other.Count = uint32(1 + r.Intn(4096))
			extra.Offers = append(extra.Offers, other)
		}
		if got := fit.Fits(need, extra); got != base {
			t.Errorf("OTHER offers moved the fit of %v from %v to %v", need, base, got)
		}
		// A place of only unknown devices fits no accelerator need, vendor-only included.
		if need != nil && need.GetVendor() != cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU {
			onlyOther := &pb.EvalPlace{Name: "p", State: known(), Offers: extra.Offers[len(place.Offers):]}
			if fit.Fits(need, onlyOther) {
				t.Errorf("a place of only unknown devices fits %v", need)
			}
		}
	})
}

func TestAVendorOnlyNeedFitsAnyKnownDeviceOfTheVendorInEnoughNumber(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		need := &cli.AcceleratorNeed{Vendor: accelerators[r.Intn(len(accelerators))], Count: uint32(1 + r.Intn(8))}
		p := pairs[r.Intn(len(pairs))]
		offer := &pb.EvalOffer{Vendor: p.vendor, Model: p.model, Count: uint32(1 + r.Intn(8))}
		place := &pb.EvalPlace{Name: "p", State: known(), Offers: []*pb.EvalOffer{offer}}
		want := offer.Vendor == need.Vendor && offer.Count >= need.Count
		if got := fit.Fits(need, place); got != want {
			t.Errorf("need %v, offer %v: %v, want %v", need, offer, got, want)
		}
	})
}

func TestANamedModelNeedsThatModel(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		p := pairs[r.Intn(len(pairs))]
		need := &cli.AcceleratorNeed{Vendor: p.vendor, Model: p.model, Count: uint32(1 + r.Intn(8))}
		q := pairs[r.Intn(len(pairs))]
		offer := &pb.EvalOffer{Vendor: q.vendor, Model: q.model, Count: uint32(1 + r.Intn(8))}
		place := &pb.EvalPlace{Name: "p", State: known(), Offers: []*pb.EvalOffer{offer}}
		want := p == q && offer.Count >= need.Count
		if got := fit.Fits(need, place); got != want {
			t.Errorf("need %v, offer %v: %v, want %v", need, offer, got, want)
		}
	})
}

func TestNamingNoModelOnlyWidensWhatFits(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		need := genNeed(r)
		if need == nil {
			return
		}
		place := genPlace(r, "p")
		wide := &cli.AcceleratorNeed{Vendor: need.Vendor, Count: need.Count}
		if fit.Fits(need, place) && !fit.Fits(wide, place) {
			t.Errorf("%v fits %v but the vendor-only need does not", need, place)
		}
	})
}

func TestTheFitIsMonotonicInCount(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		need := genNeed(r)
		place := genPlace(r, "p")
		before := fit.Fits(need, place)
		// More devices on an offer never loses a fit.
		more := &pb.EvalPlace{Name: "p", State: place.State}
		for _, o := range place.Offers {
			more.Offers = append(more.Offers, &pb.EvalOffer{Vendor: o.Vendor, Model: o.Model, Count: o.Count + uint32(r.Intn(5))})
		}
		if before && !fit.Fits(need, more) {
			t.Errorf("more devices lost the fit of %v on %v", need, place)
		}
		// A need of more devices never gains one.
		if need != nil {
			bigger := &cli.AcceleratorNeed{Vendor: need.Vendor, Model: need.Model, Count: need.Count + uint32(r.Intn(5))}
			if !before && fit.Fits(bigger, place) {
				t.Errorf("a need of more devices than %v fits %v", need, place)
			}
		}
		// The count threshold is exact for a single offer.
		if need != nil && need.Vendor != cli.AcceleratorVendor_ACCELERATOR_VENDOR_CPU && len(place.Offers) == 1 && place.GetKnown() != nil {
			o := place.Offers[0]
			sameKind := o.Vendor == need.Vendor && o.Model != cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER &&
				(need.Model == cli.AcceleratorModel_ACCELERATOR_MODEL_UNSPECIFIED || need.Model == o.Model)
			if before != (sameKind && o.Count >= need.Count) {
				t.Errorf("need %v offer %v: fit %v", need, o, before)
			}
		}
	})
}

func TestOfAgreesWithFitsPlaceByPlace(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		need := genNeed(r)
		places := genPlaces(r)
		got := fit.Of(need, places)
		var want []string
		readable := false
		for _, p := range places {
			readable = readable || p.GetKnown() != nil
			if fit.Fits(need, p) {
				want = append(want, p.Name)
			}
		}
		switch {
		case !readable:
			if got.State != pb.FitState_FIT_STATE_PLACES_UNREADABLE || len(got.Places) != 0 {
				t.Errorf("no readable place: %v", got)
			}
		case need.GetModel() == cli.AcceleratorModel_ACCELERATOR_MODEL_OTHER:
			if got.State != pb.FitState_FIT_STATE_UNKNOWN_ACCELERATOR || len(got.Places) != 0 {
				t.Errorf("need of an unknown accelerator: %v", got)
			}
		case len(want) == 0:
			if got.State != pb.FitState_FIT_STATE_NO_ACCELERATOR || len(got.Places) != 0 {
				t.Errorf("nothing fits: %v", got)
			}
		default:
			if got.State != pb.FitState_FIT_STATE_FITS || !slices.Equal(got.Places, want) {
				t.Errorf("got %v, want FITS %v", got, want)
			}
		}
		if err := wire.Validate(got); err != nil {
			t.Errorf("the fit %v is not a valid message: %v", got, err)
		}
	})
}

func TestUnreadablePlacesNeverJoinAFit(t *testing.T) {
	forAll(t, func(r *rand.Rand) {
		need := genNeed(r)
		places := genPlaces(r)
		got := fit.Of(need, places)
		for _, name := range got.Places {
			for _, p := range places {
				if p.Name == name && p.GetKnown() == nil {
					t.Errorf("unreadable place %s is in the fit %v", name, got)
				}
			}
		}
		// Adding an unreadable place changes nothing but the all-unreadable case.
		withUnreadable := append(slices.Clone(places), &pb.EvalPlace{Name: "z", Kind: pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH, State: unreadable()})
		if after := fit.Of(need, withUnreadable); after.State != got.State || !slices.Equal(after.Places, got.Places) {
			t.Errorf("an unreadable place moved the fit from %v to %v", got, after)
		}
	})
}
