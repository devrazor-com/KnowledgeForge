"""config.environments() — the cross-platform parsing contract for the configured
Module 3 target-environment list. Fail-closed (no synthetic fallback); utf-8-sig; LF/CRLF;
strip per line only; blank + full-line '#' comments ignored; duplicates are an error that
reports the PHYSICAL file line number; names verbatim and in file order.
"""
import pytest

from workbench import config


def _write(tmp_path, data: bytes, monkeypatch):
    p = tmp_path / "environments.txt"
    p.write_bytes(data)
    monkeypatch.setenv("WORKBENCH_ENVIRONMENTS_FILE", str(p))
    return p


def test_unset_is_fail_closed(monkeypatch):
    monkeypatch.delenv("WORKBENCH_ENVIRONMENTS_FILE", raising=False)
    with pytest.raises(config.EnvironmentsConfigError) as ei:
        config.environments()
    assert ei.value.kind == "unset"


def test_missing_or_unreadable_file_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("WORKBENCH_ENVIRONMENTS_FILE", str(tmp_path / "does-not-exist.txt"))
    with pytest.raises(config.EnvironmentsConfigError) as ei:
        config.environments()
    assert ei.value.kind == "unreadable"


def test_empty_or_all_comments_is_fail_closed(tmp_path, monkeypatch):
    _write(tmp_path, b"# only a comment\n\n   \n", monkeypatch)
    with pytest.raises(config.EnvironmentsConfigError) as ei:
        config.environments()
    assert ei.value.kind == "empty"


def test_duplicate_is_error_with_physical_line_number(tmp_path, monkeypatch):
    # blanks/comments on lines 1-2 so the duplicate is physically on line 5, not index 2.
    _write(tmp_path, b"# comment\n\nidr\nwebplus\nidr\n", monkeypatch)
    with pytest.raises(config.EnvironmentsConfigError) as ei:
        config.environments()
    assert ei.value.kind == "duplicate"
    assert "line 5" in ei.value.message and "line 3" in ei.value.message   # dup at 5, first at 3


def test_utf8_bom_crlf_comments_and_whitespace_are_handled_verbatim(tmp_path, monkeypatch):
    # UTF-8 BOM, CRLF endings, a leading full-line comment, surrounding whitespace, a blank.
    _write(tmp_path, "﻿# header\r\n  ifastbase  \r\nwebplus\r\n\r\nrdsp-services\r\n".encode("utf-8"),
           monkeypatch)
    # BOM must not contaminate the first name; whitespace stripped; order preserved.
    assert config.environments() == ["ifastbase", "webplus", "rdsp-services"]


def test_internal_characters_are_not_normalised(tmp_path, monkeypatch):
    _write(tmp_path, b"ics-config\nRDSP-Services\n", monkeypatch)   # hyphens + mixed case kept
    assert config.environments() == ["ics-config", "RDSP-Services"]   # no case-fold, no alias
