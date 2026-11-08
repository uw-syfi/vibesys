package clidaemon

import (
	"context"
	"errors"
	"fmt"

	"buf.build/go/protovalidate"

	cli "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
)

// PlacementLister is the call of the CLI daemon that reports its placements and the hardware each gives.
type PlacementLister interface {
	ListPlacements(context.Context, *cli.ListPlacementsRequest) (*cli.ListPlacementsResponse, error)
}

// PlacementConnection is a PlacementLister the caller closes.
type PlacementConnection interface {
	PlacementLister
	Close() error
}

// PlacementConnector dials the socket the tool answered.
type PlacementConnector func(context.Context, Socket) (PlacementConnection, error)

// ListPlacements calls the daemon. The caller's context must carry a deadline: a probe is bounded by it.
func (c *Client) ListPlacements(ctx context.Context, r *cli.ListPlacementsRequest) (*cli.ListPlacementsResponse, error) {
	if _, ok := ctx.Deadline(); !ok {
		return nil, errors.New("ListPlacements requires a caller deadline")
	}
	return c.svc.ListPlacements(ctx, r)
}

// Places reads the eval places of a host from its CLI daemon: it asks the tool where the daemon is,
// connects, asks for the placements and closes. The daemon is the CLI's, so a place is reported in the
// orchestrator's own message, whatever the CLI's contract says.
type Places struct {
	Ensurer Ensurer
	Connect PlacementConnector
}

// List returns the places in the order the daemon reports them. Refresh asks the daemon to probe now.
func (p Places) List(ctx context.Context, refresh bool) ([]*pb.EvalPlace, error) {
	socket, err := p.Ensurer.Ensure(ctx)
	if err != nil {
		return nil, err
	}
	conn, err := p.Connect(ctx, socket)
	if err != nil {
		return nil, err
	}
	defer conn.Close()
	return EvalPlaces(ctx, conn, refresh)
}

// EvalPlaces asks a daemon for its placements and maps each to an eval place.
func EvalPlaces(ctx context.Context, svc PlacementLister, refresh bool) ([]*pb.EvalPlace, error) {
	resp, err := svc.ListPlacements(ctx, &cli.ListPlacementsRequest{Refresh: refresh})
	if err != nil {
		return nil, err
	}
	if err := protovalidate.Validate(resp); err != nil {
		return nil, fmt.Errorf("the CLI daemon's placements are not valid: %w", err)
	}
	places := make([]*pb.EvalPlace, 0, len(resp.GetPlacements()))
	for _, view := range resp.GetPlacements() {
		place, err := EvalPlace(view)
		if err != nil {
			return nil, err
		}
		places = append(places, place)
	}
	return places, nil
}

// EvalPlace maps one placement to an eval place.
//
//   - A placement the daemon could not reach is unreadable as UNREACHABLE, with its reason, whatever
//     offers it still holds: they fit nothing.
//   - A placement that is not probed keeps its declared offers: they are known, from DECLARED.
//   - A placement whose hardware is unknown is unreadable, with the reason the daemon gave.
//
// The result is validated, and an error names the placement.
func EvalPlace(view *cli.PlacementView) (*pb.EvalPlace, error) {
	kind, ok := placeKinds[view.GetKind()]
	if !ok {
		return nil, fmt.Errorf("placement %q: kind %v has no eval place kind", view.GetName(), view.GetKind())
	}
	place := &pb.EvalPlace{Name: view.GetName(), Kind: kind}
	switch {
	case view.GetReach().GetState() == cli.ReachState_REACH_STATE_UNREACHABLE:
		place.State = &pb.EvalPlace_Unreadable{Unreadable: &pb.OffersUnreadable{
			Reason: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREACHABLE, Detail: view.GetReach().GetReason()}}
	case view.GetUnknown() != nil:
		reason, ok := unreadableReasons[view.GetUnknown().GetReason()]
		if !ok {
			return nil, fmt.Errorf("placement %q: unknown offers reason %v has no eval place reason", view.GetName(), view.GetUnknown().GetReason())
		}
		place.State = &pb.EvalPlace_Unreadable{Unreadable: &pb.OffersUnreadable{Reason: reason, Detail: view.GetUnknown().GetDetail()}}
	default:
		known := view.GetKnown()
		source, ok := offerSources[known.GetSource()]
		if !ok {
			return nil, fmt.Errorf("placement %q: offer source %v has no eval place source", view.GetName(), known.GetSource())
		}
		place.State = &pb.EvalPlace_Known{Known: &pb.OffersKnown{Source: source, ProbedAt: known.GetProbedAt()}}
		for _, offer := range known.GetOffers() {
			place.Offers = append(place.Offers, &pb.EvalOffer{
				Vendor: offer.GetVendor(), Model: offer.GetModel(), Count: offer.GetCount(), DeviceName: offer.GetDeviceName()})
		}
	}
	if err := protovalidate.Validate(place); err != nil {
		return nil, fmt.Errorf("placement %q is not a valid eval place: %w", view.GetName(), err)
	}
	return place, nil
}

var placeKinds = map[cli.PlacementKind]pb.EvalPlaceKind{
	cli.PlacementKind_PLACEMENT_KIND_LOCAL:     pb.EvalPlaceKind_EVAL_PLACE_KIND_LOCAL,
	cli.PlacementKind_PLACEMENT_KIND_SSH:       pb.EvalPlaceKind_EVAL_PLACE_KIND_SSH,
	cli.PlacementKind_PLACEMENT_KIND_SLURM:     pb.EvalPlaceKind_EVAL_PLACE_KIND_SLURM,
	cli.PlacementKind_PLACEMENT_KIND_APPTAINER: pb.EvalPlaceKind_EVAL_PLACE_KIND_APPTAINER,
}

var offerSources = map[cli.OfferSource]pb.OffersSource{
	cli.OfferSource_OFFER_SOURCE_PROBED:   pb.OffersSource_OFFERS_SOURCE_PROBED,
	cli.OfferSource_OFFER_SOURCE_DECLARED: pb.OffersSource_OFFERS_SOURCE_DECLARED,
}

var unreadableReasons = map[cli.OffersUnknownReason]pb.OffersUnreadableReason{
	cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREACHABLE:   pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREACHABLE,
	cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_UNREADABLE:    pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_UNREADABLE,
	cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_TIMED_OUT:     pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_TIMED_OUT,
	cli.OffersUnknownReason_OFFERS_UNKNOWN_REASON_NOT_PROBEABLE: pb.OffersUnreadableReason_OFFERS_UNREADABLE_REASON_NOT_PROBEABLE,
}
