"""The cosign public keys the platform hands a station at enrolment."""

from backend.api import enrolment
from backend.core.config import settings

_PEM = "-----BEGIN PUBLIC KEY-----\n{}\n-----END PUBLIC KEY-----\n"


def test_it_reads_every_pub_and_ignores_other_files(tmp_path, monkeypatch):
    (tmp_path / "cosign.pub").write_text(_PEM.format("AAA"))
    (tmp_path / "previous.pub").write_text(_PEM.format("BBB"))  # rotation overlap
    (tmp_path / "notes.txt").write_text("not a key")
    monkeypatch.setattr(settings, "update_signing_keys_dir", str(tmp_path))

    keys = enrolment._update_signing_keys()
    assert len(keys) == 2
    assert all("PUBLIC KEY" in key for key in keys)


def test_a_missing_directory_hands_out_nothing(tmp_path, monkeypatch):
    # No keys means updates cannot be verified and so will not run — the safe
    # default, not an error at enrolment time.
    monkeypatch.setattr(settings, "update_signing_keys_dir", str(tmp_path / "absent"))
    assert enrolment._update_signing_keys() == []
