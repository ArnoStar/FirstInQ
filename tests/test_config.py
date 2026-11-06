import pytest
from cryptography.fernet import Fernet

from app.config import ConfigError, load_settings
from app.security import SessionCipher, key_from_secret


def test_generated_key_is_used_as_is():
    key = Fernet.generate_key().decode()
    assert key_from_secret(key) == key.encode()


def test_any_string_works_as_secret_key():
    secret = "some long random string that is not a fernet key!!"
    key = key_from_secret(secret)
    assert key == key_from_secret(secret)  # stable across restarts
    assert key != key_from_secret(secret + "x")
    cipher = SessionCipher(key)
    assert cipher.decrypt(cipher.encrypt("session")) == "session"


@pytest.mark.parametrize("api_id, api_hash, message", [
    ("", "", "must be set"),
    ("123456", "0123456789abcdef0123456789abcdef", "example values"),
    ("12ab", "f" * 32, "numeric"),
])
def test_bad_credentials_are_rejected_clearly(monkeypatch, api_id, api_hash, message):
    monkeypatch.setenv("TG_API_ID", api_id)
    monkeypatch.setenv("TG_API_HASH", api_hash)
    with pytest.raises(ConfigError, match=message):
        load_settings()


async def test_changed_secret_key_logs_user_out(tmp_path):
    from app.config import Settings
    from app.db import Database
    from app.telegram import NotAuthorized, TelegramAccounts

    db = Database(tmp_path / "t.sqlite3")
    await db.connect()
    old = SessionCipher(key_from_secret("the old secret key, long enough"))
    await db.upsert_user(id=7, first_name="A", last_name="", username=None, phone=None,
                         session_enc=old.encrypt("some session"))
    settings = Settings(api_id=1, api_hash="x", secret_key=None, data_dir=tmp_path,
                        cookie_secure=False, web_session_days=1, missed_grace_seconds=60)
    accounts = TelegramAccounts(settings, db, SessionCipher(key_from_secret("a new secret key, also long")))
    revoked = []

    async def on_revoked(user_id):
        revoked.append(user_id)

    accounts.on_revoked = on_revoked
    with pytest.raises(NotAuthorized):
        await accounts.get(7)
    assert revoked == [7]
    await accounts.log_out(7)  # must not raise either
    await db.close()
