"""One-time copy of pre-rename (clau-decode) config + DB into agent-decoder dirs."""

from agent_decoder import config


def test_legacy_data_dir_db_and_config_are_copied(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    old = tmp_path / "data" / "clau-decode"
    old.mkdir(parents=True)
    (old / "index.db").write_bytes(b"DB")
    (old / "index.db-wal").write_bytes(b"WAL")
    oldc = tmp_path / "cfg" / "clau-decode"
    oldc.mkdir(parents=True)
    (oldc / "config.json").write_text("{}")

    db = config.get_db_path()
    assert db == tmp_path / "data" / "agent-decoder" / "index.db"
    assert db.read_bytes() == b"DB"
    assert (db.parent / "index.db-wal").read_bytes() == b"WAL"
    assert (old / "index.db").exists()  # backstop left in place

    cfg = config.get_config_path()
    assert cfg.read_text() == "{}" and (oldc / "config.json").exists()


def test_existing_new_db_is_never_overwritten(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    (tmp_path / "clau-decode").mkdir()
    (tmp_path / "clau-decode" / "index.db").write_bytes(b"OLD")
    (tmp_path / "agent-decoder").mkdir()
    (tmp_path / "agent-decoder" / "index.db").write_bytes(b"NEW")
    assert config.get_db_path().read_bytes() == b"NEW"
