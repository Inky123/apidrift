import com.stripe.Stripe;
import com.stripe.model.Charge;

class Billing {
    Charge charge(java.util.Map<String, Object> params) throws Exception {
        return Charge.create(params);
    }
}
