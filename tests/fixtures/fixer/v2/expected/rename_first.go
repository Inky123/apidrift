package billing

import (
	"context"

	"github.com/stripe/stripe-go/v86"
	"github.com/stripe/stripe-go/v86/financialconnections/session"
)

func literal() (*stripe.FinancialConnectionsSession, error) {
	return session.New(&stripe.FinancialConnectionsSessionParams{
		AccountHolder: &stripe.FinancialConnectionsSessionAccountHolderParams{
			Type:     stripe.String("customer"),
			Customer: stripe.String("cus_123"),
		},
		Permissions: stripe.StringSlice([]string{"balances"}),
		Filters: &stripe.FinancialConnectionsSessionFiltersParams{
			Country: stripe.String("US"),
		},
	})
}

func pointers(ctx context.Context, sc *stripe.Client, country string) (*stripe.FinancialConnectionsSession, error) {
	params := &stripe.FinancialConnectionsSessionCreateParams{
		Permissions: []*string{stripe.String("ownership")},
		Filters:     &stripe.FinancialConnectionsSessionCreateFiltersParams{Country: stripe.String(country)},
	}
	return sc.V1FinancialConnectionsSessions.Create(ctx, params)
}
