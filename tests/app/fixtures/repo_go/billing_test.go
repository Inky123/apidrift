package billing

import "testing"

func TestCharge(t *testing.T) {
	stub := "https://api.stripe.com/v1/charges"
	lookup := "https://api.stripe.com/v1/charges/ch_1"
	if stub == lookup {
		t.Fatal("unreachable")
	}
}
