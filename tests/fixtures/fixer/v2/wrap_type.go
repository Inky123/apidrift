package billing

import (
	"github.com/stripe/stripe-go/v86"
	"github.com/stripe/stripe-go/v86/invoice"
	"github.com/stripe/stripe-go/v86/subscription"
)

func resume(id string) (*stripe.Subscription, error) {
	return subscription.Resume(id, &stripe.SubscriptionResumeParams{
		BillingCycleAnchor: stripe.String("now"),
		ProrationBehavior:  stripe.String("create_prorations"),
	})
}

func resumeChosen(id, anchor string) (*stripe.Subscription, error) {
	params := &stripe.SubscriptionResumeParams{}
	params.BillingCycleAnchor = stripe.String(anchor)
	return subscription.Resume(id, params)
}

func preview(customer string) (*stripe.Invoice, error) {
	return invoice.CreatePreview(&stripe.InvoiceCreatePreviewParams{
		Customer: stripe.String(customer),
		SubscriptionDetails: &stripe.InvoiceCreatePreviewSubscriptionDetailsParams{
			BillingCycleAnchor: stripe.Int64(1767225600),
		},
	})
}

func previewNow(customer string) (*stripe.Invoice, error) {
	return invoice.CreatePreview(&stripe.InvoiceCreatePreviewParams{
		Customer: stripe.String(customer),
		SubscriptionDetails: &stripe.InvoiceCreatePreviewSubscriptionDetailsParams{
			BillingCycleAnchorNow: stripe.Bool(true),
		},
	})
}
