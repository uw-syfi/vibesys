package main

import (
	"fmt"
	"os"

	accuracytrainticket "vibesys/microservice-evaluator/accuracyapps/trainticket"
	"vibesys/microservice-evaluator/apps/declarative"
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
		composition.Application("train-ticket", benchmarktrainticket.New),
		composition.AccuracyApplication("train-ticket", accuracytrainticket.New),
	)
	if err != nil {
		fmt.Fprintln(os.Stderr, "servicebench:", err)
		os.Exit(1)
	}
}
