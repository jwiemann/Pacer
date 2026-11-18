import pytest

import openrouter_keys
import secret_store


def test_key_roundtrips_encrypted_and_only_the_ends_are_shown(tmp_path):
    path = str(tmp_path / "k.key")
    key = "sk-or-v1-" + "a1b2c3d4" * 6
    secret_store.save(path, key)
    raw = open(path, "rb").read()
    assert key.encode() not in raw and b"a1b2c3d4" not in raw  # not readable in the file
    assert secret_store.load(path) == key
    hint = secret_store.mask(key)
    assert hint == key[:12] + "..." + key[-4:] and len(hint) < len(key) // 2


def test_garbage_is_not_stored_and_a_damaged_file_reads_as_no_key(tmp_path):
    path = str(tmp_path / "k.key")
    with pytest.raises(ValueError):
        secret_store.save(path, "short")
    open(path, "wb").write(b"not dpapi")
    assert secret_store.load(path) is None
    assert secret_store.load(str(tmp_path / "missing")) is None


def test_key_listing_reading_keeps_limit_and_spend():
    r = openrouter_keys.key_reading({"hash": "h1", "name": "agents", "limit": 10, "limit_remaining": 2.5, "limit_reset": None,
                                     "usage": 40, "usage_daily": 1.5, "disabled": False})
    assert r["used"] == 75.0 and r["spent"] == 7.5 and r["name"] == "agents" and r["daily"] == 1.5 and not r["unlimited"]
    free = openrouter_keys.key_reading({"hash": "h2", "name": "x", "limit": None, "usage": 3})
    assert free["unlimited"] and free["usage"] == 3
