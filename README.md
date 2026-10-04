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

Use the admin token to create a show after running the MySQL migrations:

```bash
curl -X POST http://127.0.0.1:8000/shows \
	-H "Authorization: Bearer <admin-token>" \
	-H "Content-Type: application/json" \
	-d '{"name":"friday-night","seats":["A1","A2"],"price_paise":25000}'
```
