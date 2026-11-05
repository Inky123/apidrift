package billing

import (
	"context"
	"net/http"

	"github.com/stripe/stripe-go/v86"
	"github.com/stripe/stripe-go/v86/charge"
	"github.com/stripe/stripe-go/v86/client"
)

// a struct literal at the call: every field is visible
func Charge(amount int64, note string) (*stripe.Charge, error) {
	params := &stripe.ChargeParams{
		Amount:      stripe.Int64(amount),
		Currency:    stripe.String("usd"),
		Description: stripe.String(note),
	}
	return charge.New(params)
}

func Lookup(id string) (string, error) {
	found, err := charge.Get(id, nil)
	if err != nil {
		return "", err
	}
	return found.ReceiptURL, nil
}

// the params arrive from elsewhere: nothing is visible here
func ViaClient(sc *client.API, params *stripe.ChargeParams) (*stripe.Charge, error) {
	return sc.Charges.New(params)
}

func ViaService(ctx context.Context, key string, city string) (*stripe.Charge, error) {
	sc := stripe.NewClient(key)
	return sc.V1Charges.Create(ctx, &stripe.ChargeCreateParams{
		Amount:   stripe.Int64(500),
		Currency: stripe.String("usd"),
		Shipping: &stripe.ChargeCreateShippingParams{Address: &stripe.AddressParams{City: stripe.String(city)}},
	})
}

func Capture(id string) (*stripe.Charge, error) {
	return charge.Capture(id, &stripe.ChargeCaptureParams{Amount: stripe.Int64(5)})
}

// a raw call next to the SDK ones
func Raw(key string) (*http.Response, error) {
	req, err := http.NewRequest("POST", "https://api.stripe.com/v1/charges", nil)
	if err != nil {
		return nil, err
	}
	req.SetBasicAuth(key, "")
	return http.DefaultClient.Do(req)
}
