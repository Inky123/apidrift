require 'sinatra'
require 'stripe'

Stripe.api_key = ENV['STRIPE_SECRET_KEY']

post '/charge' do
  charge = Stripe::Charge.create(amount: params[:amount], currency: 'usd', description: "order #{params[:order_id]}")
  { id: charge.id }.to_json
end

get '/charge/:id' do
  Stripe::Charge.retrieve(params[:id]).to_json
end
