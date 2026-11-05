require 'webmock/rspec'

describe 'charges' do
  it 'creates one' do
    stub_request(:post, 'https://api.stripe.com/v1/charges').to_return(body: '{}')
    stub_request(:get, 'https://api.stripe.com/v1/charges/ch_9').to_return(body: '{}')
  end
end
