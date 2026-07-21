package trainticket

import (
	"context"
	"crypto/sha256"
	"errors"
	"fmt"
	"math/rand"

	"vibesys/microservice-evaluator/accuracy"
	"vibesys/microservice-evaluator/api"
)

func (a *Application) Check(
	ctx context.Context,
	runtime api.Runtime,
	check api.AccuracyContext,
	recorder api.AccuracyRecorder,
) (checkErr error) {
	client, err := newClient(runtime, a.timeout)
	if err != nil {
		return err
	}
	journal := accuracy.NewJournal()
	defer func() {
		cleanupErr := journal.Cleanup(context.WithoutCancel(ctx))
		if cleanupErr != nil {
			checkErr = errors.Join(checkErr, fmt.Errorf("fixture cleanup: %w", cleanupErr))
		}
	}()

	random := rand.New(rand.NewSource(check.Seed ^ 0x5eed5eed))
	namespaceDigest := sha256.Sum256([]byte(fmt.Sprintf("%d/accuracy", check.Seed)))
	namespace := fmt.Sprintf("%x", namespaceDigest[:12])
	cases := make([]*graphCase, 0, check.Cases)
	for index := 0; index < check.Cases; index++ {
		cases = append(cases, makeCase(random, namespace, index))
	}

	checks, err := a.verifyProtocol(ctx, client)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := pass(recorder, "protocol_contract", "persistent_http"); err != nil {
		return err
	}

	checks, err = a.verifySeedCatalog(ctx, client)
	recorder.AddChecks(checks)
	if err != nil {
		return err
	}
	if err := pass(recorder, "exact_seed_catalog", "strict_entity_schemas"); err != nil {
		return err
	}

	creationOrder := shuffledCases(random, cases)
	live := make([]*graphCase, 0, len(cases))
	for _, item := range creationOrder {
		if err := checkContext(ctx); err != nil {
			return err
		}
		checks, err = a.createCase(ctx, client, journal, item)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		live = append(live, item)
		checks, err = a.verifyExactState(ctx, client, live)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		checks, err = a.verifyCase(ctx, client, item, random)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
	}
	if err := pass(
		recorder,
		"randomized_crud_graph",
		"cross_entity_graph",
		"read_your_write",
	); err != nil {
		return err
	}

	for _, item := range shuffledCases(random, cases) {
		checks, err = a.updateCase(ctx, client, item, random)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		checks, err = a.verifyCase(ctx, client, item, random)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		checks, err = a.verifyExactState(ctx, client, cases)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
	}
	if err := pass(recorder, "updates_visible", "stale_secondary_indexes"); err != nil {
		return err
	}

	if check.Restart != nil {
		if err := check.Restart(ctx); err != nil {
			return err
		}
		for _, item := range shuffledCases(random, cases) {
			checks, err = a.verifyCase(ctx, client, item, random)
			recorder.AddChecks(checks)
			if err != nil {
				return fmt.Errorf("post-crash persistence: %w", err)
			}
		}
		checks, err = a.verifyExactState(ctx, client, cases)
		recorder.AddChecks(checks)
		if err != nil {
			return fmt.Errorf("post-crash exact state: %w", err)
		}
		if err := recorder.Pass("crash_recovery"); err != nil {
			return err
		}
	}

	live = append([]*graphCase(nil), cases...)
	for _, item := range shuffledCases(random, cases) {
		checks, err = a.deleteCase(ctx, client, item, random)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		checks, err = a.verifyDeleted(ctx, client, item)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		live = removeCase(live, item)
		checks, err = a.verifyExactState(ctx, client, live)
		recorder.AddChecks(checks)
		if err != nil {
			return err
		}
		for _, survivor := range shuffledCases(random, live) {
			checks, err = a.verifyCase(ctx, client, survivor, random)
			recorder.AddChecks(checks)
			if err != nil {
				return fmt.Errorf("surviving runtime graph %d: %w", survivor.index, err)
			}
		}
		if err := dismissCase(journal, item); err != nil {
			return err
		}
	}
	if journal.Active() != 0 {
		return fmt.Errorf("accuracy fixture journal retained %d entries after verified deletion", journal.Active())
	}
	checks, err = a.verifyExactState(ctx, client, nil)
	recorder.AddChecks(checks)
	if err != nil {
		return fmt.Errorf("final seed restoration: %w", err)
	}
	return pass(recorder, "deletes_visible", "delete_isolation")
}

func shuffledCases(random *rand.Rand, source []*graphCase) []*graphCase {
	shuffled := append([]*graphCase(nil), source...)
	random.Shuffle(len(shuffled), func(left, right int) {
		shuffled[left], shuffled[right] = shuffled[right], shuffled[left]
	})
	return shuffled
}

func removeCase(cases []*graphCase, remove *graphCase) []*graphCase {
	for index, item := range cases {
		if item == remove {
			return append(cases[:index], cases[index+1:]...)
		}
	}
	return cases
}
