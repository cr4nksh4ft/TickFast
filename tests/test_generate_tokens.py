import os
import sys
from datetime import UTC, datetime, timedelta

import jwt

from scripts import generate_tokens
from scripts.credentials import BurstUser


def test_generate_tokens_writes_private_files_without_printing_tokens(
    monkeypatch, tmp_path, capsys
):
    output_dir = tmp_path / "credentials"
    admin_token = jwt.encode(
        {"sub": "9", "exp": datetime.now(UTC) + timedelta(days=30)},
        "test-secret-with-at-least-32-bytes-long",
        algorithm="HS256",
    )
    user_tokens = ["user-token-1", "user-token-2"]

    def prepare_credentials(tokens_path, user_count, allow_non_test_database):
        assert user_count == 2
        assert allow_non_test_database
        tokens_path.write_text("\n".join(user_tokens) + "\n", encoding="utf-8")
        os.chmod(tokens_path, 0o600)
        return [BurstUser(1, user_tokens[0]), BurstUser(2, user_tokens[1])], admin_token

    monkeypatch.setattr(generate_tokens, "prepare_credentials", prepare_credentials)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "generate_tokens",
            "--users",
            "2",
            "--output-dir",
            str(output_dir),
            "--allow-non-test-database",
        ],
    )

    assert generate_tokens.main() == 0

    admin_file = output_dir / "admin.env"
    users_file = output_dir / "users.tokens"
    assert admin_file.read_text(encoding="utf-8") == f"ADMIN_TOKEN={admin_token}\n"
    assert users_file.read_text(encoding="utf-8") == "user-token-1\nuser-token-2\n"
    assert output_dir.stat().st_mode & 0o777 == 0o700
    assert admin_file.stat().st_mode & 0o777 == 0o600
    assert users_file.stat().st_mode & 0o777 == 0o600
    output = capsys.readouterr().out
    assert admin_token not in output
    assert "Token lifetime: 30 days" in output


def test_generate_tokens_requires_two_users(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["generate_tokens", "--users", "1"])

    try:
        generate_tokens.main()
    except SystemExit as error:
        assert error.code == 2
    else:
        raise AssertionError("expected argparse to reject fewer than two users")


def test_generate_tokens_restricts_existing_directory_permissions(
    tmp_path, monkeypatch
):
    output_dir = tmp_path / "credentials"
    output_dir.mkdir(mode=0o755)
    os.chmod(output_dir, 0o755)

    def stop_before_writing_tokens(*args):
        raise RuntimeError("credential generation reached")

    monkeypatch.setattr(
        generate_tokens, "prepare_credentials", stop_before_writing_tokens
    )
    try:
        generate_tokens.generate_credentials(2, output_dir, False)
    except RuntimeError as error:
        assert "credential generation reached" in str(error)
    else:
        raise AssertionError("expected token generation to reach the mocked stop")

    assert output_dir.stat().st_mode & 0o777 == 0o700