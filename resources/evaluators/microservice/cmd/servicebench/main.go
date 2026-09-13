package main

import (
	"fmt"
	"os"

	accuracyhotel "vibesys/microservice-evaluator/accuracyapps/hotel"
	accuracytrainticket "vibesys/microservice-evaluator/accuracyapps/trainticket"
	"vibesys/microservice-evaluator/apps/declarative"
	benchmarkhotel "vibesys/microservice-evaluator/apps/hotel"
	"vibesys/microservice-evaluator/apps/socialnetwork"
	benchmarktrainticket "vibesys/microservice-evaluator/apps/trainticket"
	"vibesys/microservice-evaluator/composition"
	"vibesys/microservice-evaluator/drivers/httpdriver"
	"vibesys/microservice-evaluator/servicebenchcli"
)

var version = "dev"

func main() {
	err := servicebenchcli.Run(
		os.Args[1:],
		version,
		composition.Driver(httpdriver.New()),
		composition.Application("declarative", declarative.New),
		composition.Application("hotel", benchmarkhotel.New),
		composition.Application("social-network", socialnetwork.New),
		composition.Application("train-ticket", benchmarktrainticket.New),
		composition.AccuracyApplication("hotel", accuracyhotel.New),
		composition.AccuracyApplication("train-ticket", accuracytrainticket.New),
	)
	if err != nil {
		fmt.Fprintln(os.Stderr, "servicebench:", err)
		os.Exit(1)
	}
}
