"""Real fixture login for pre-P3 HTTP behavior checks; no dependency override."""


async def login_customer(client, app=None):
    app = app or client._transport.app
    await app.state.auth.create_account('http@fixture.invalid', 'fixture-http-password', 'customer', 1)
    response = await client.post('/auth/login', json={
        'email': 'http@fixture.invalid', 'password': 'fixture-http-password'})
    assert response.status_code == 200
    client.headers['Authorization'] = 'Bearer ' + response.json()['access_token']
