# TickFast

## Local Authentication

Set `JWT_SECRET` in your ignored `.env` file. Generate a local secret with:

```bash
python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Apply migrations, then create user rows locally. MySQL assigns each user's
stable integer ID; the role is stored on that row:

```bash
uv run python -m migrations
uv run python -m scripts.create_user --role user
uv run python -m scripts.create_user --role admin
```

Use the printed `user_id` to mint a one-hour token. The CLI loads the row and
uses its ID and stored role; there is no public signup, login, or token-issuance
endpoint:

```bash
uv run python -m scripts.mint_token --user-id <user-id>
```

Store the minted admin token in the ignored `.env` as `ADMIN_TOKEN`. With the
API running, the local helper reads that setting and sends it as a bearer token
to create the show:

```bash
uv run python -m scripts.create_show \
	--name friday-night \
	--price-paise 25000 \
	--seats A1 A2 A3 B1 B2 B3
```

Pass the assigned seat labels directly. Labels must be unique, nonblank, and
no longer than 255 characters.

The helper defaults to `http://127.0.0.1:8000`; set `TICKFAST_API_URL` in
`.env` to use another local API address. The API still verifies the JWT with
`JWT_SECRET`; it does not compare against `ADMIN_TOKEN`.
