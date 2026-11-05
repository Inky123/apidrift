package billing

import (
	"context"

	"github.com/stripe/stripe-go/v86"
	"github.com/stripe/stripe-go/v86/client"
	"github.com/stripe/stripe-go/v86/paymentmethod"
)

func inline(id string) {
	paymentmethod.Update(id, &stripe.PaymentMethodParams{Metadata: map[string]string{"order": "1"}})
}

func lastEntry(sc *client.API, id string) {
	sc.PaymentMethods.Update(id, &stripe.PaymentMethodParams{Metadata: map[string]string{"order": "2"}})
}

func ownLines(ctx context.Context, sc *stripe.Client, id string) error {
	params := &stripe.PaymentMethodUpdateParams{
		Metadata: map[string]string{"order": "3"}, // keep this comment
	}
	_, err := sc.V1PaymentMethods.Update(ctx, id, params)
	return err
}

func assigned(id string) {
	params := &stripe.PaymentMethodParams{}
	params.Metadata = map[string]string{"order": "4"}
	paymentmethod.Update(id, params)
}
