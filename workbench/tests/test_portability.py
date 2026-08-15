r"""Cross-platform portability guarantees (Windows readiness).

Two independent concerns, both platform-neutral to assert:

1. Fingerprint stability across line endings. The knowledge fingerprint (and the
   task fingerprint) must be identical whether Markdown / task JSON arrives with
   Unix LF or Windows CRLF endings — otherwise merely transferring a package from
   macOS to Windows (where Git or an editor may rewrite endings) would move the
   fingerprint, silently invalidating every prior run and approval. Content is
   normalised to \n before hashing, so this holds without OS-specific code.

2. Physical directory identity. The same physical package directory must not be
   registerable twice merely because it was reached through two different textual
   paths. A resolved path string cannot express this on its own: on a
   case-insensitive filesystem (default macOS APFS, Windows) two differently-cased
   spellings resolve to different strings yet the same directory, and on Windows a
   junction / 8.3 name / mapped-drive-vs-UNC path is the same directory under
   different text. `same_physical_dir` compares filesystem identity via
   `os.path.samefile`, which sees through all of those; a missing/unreadable path is
   a non-match rather than an error, so a broken source never blocks registration.
   `normalize_root` still just resolves to a human-readable machine-local string.
"""

import json
import os

import pytest

from workbench.fingerprints import normalize_content, package_fingerprint, task_fingerprint
from workbench.packages import normalize_root, same_physical_dir


# --------------------------------------------------------------------------
# 1. LF vs CRLF → identical fingerprints
# --------------------------------------------------------------------------

def _files(text):
    return [{"path": "a.md", "content": normalize_content(text)}]


def test_package_fingerprint_is_line_ending_agnostic():
    lf = "# Title\n\nintro paragraph\n\n- one\n- two\n"
    crlf = lf.replace("\n", "\r\n")
    cr = lf.replace("\n", "\r")                       # old-Mac CR, for good measure
    fp_lf = package_fingerprint(_files(lf))
    assert package_fingerprint(_files(crlf)) == fp_lf
    assert package_fingerprint(_files(cr)) == fp_lf


def test_package_fingerprint_still_reflects_real_content_change():
    """Guard: the normalisation must not be so aggressive it erases genuine edits."""
    a = package_fingerprint(_files("line one\nline two\n"))
    b = package_fingerprint(_files("line one\nline TWO\n"))
    assert a != b


def test_task_fingerprint_is_line_ending_agnostic():
    """A task JSON file rewritten with CRLF endings (whitespace between tokens)
    parses to the same object, so the task fingerprint is unchanged."""
    task_lf = ('{\n  "id": "T1",\n  "description": "do the thing",\n'
               '  "acceptance_criteria": "it is done",\n  "checks": []\n}\n')
    task_crlf = task_lf.replace("\n", "\r\n")
    obj_lf, obj_crlf = json.loads(task_lf), json.loads(task_crlf)
    assert task_fingerprint(obj_lf) == task_fingerprint(obj_crlf)


# --------------------------------------------------------------------------
# 2. Physical directory identity
# --------------------------------------------------------------------------

def _fs_is_case_insensitive(tmp_path):
    """Probe: does this filesystem treat two cases of a name as the same directory?"""
    d = tmp_path / "CaseProbe"
    d.mkdir()
    return (tmp_path / "caseprobe").is_dir()


def test_normalize_root_collapses_dot_and_redundant_separators(tmp_path):
    """`.`, `..` and doubled separators canonicalise to one form — platform-neutral."""
    d = tmp_path / "pkgs" / "claims"
    d.mkdir(parents=True)
    straight = normalize_root(str(d))
    via_dot = normalize_root(str(tmp_path / "pkgs" / "." / "claims"))
    via_updown = normalize_root(str(tmp_path / "pkgs" / "claims" / ".." / "claims"))
    assert straight == via_dot == via_updown


def test_normalize_root_stores_a_plain_resolved_path(tmp_path):
    """`normalize_root` stores the resolved machine-local path verbatim — no case
    folding, no lossy canonicalisation. Physical identity is NOT baked into the stored
    string; it is established separately by `same_physical_dir`."""
    d = tmp_path / "Claims"
    d.mkdir()
    assert normalize_root(str(d)) == str(d.resolve())


def test_same_physical_dir_sees_through_symlinks(tmp_path):
    """A symlink and its target are the same physical directory — everywhere."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    try:
        os.symlink(real, link)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation not permitted on this platform/config")
    assert same_physical_dir(str(real), str(link))
    assert same_physical_dir(str(real), str(real))              # reflexive


def test_same_physical_dir_missing_path_is_a_nonmatch_not_an_error(tmp_path):
    """A path that cannot be stat'ed yields False (never raises) — a broken registered
    source must never block registering a different package."""
    real = tmp_path / "real"
    real.mkdir()
    gone = tmp_path / "does-not-exist"
    assert same_physical_dir(str(real), str(gone)) is False
    assert same_physical_dir(str(gone), str(gone)) is False


def test_same_physical_dir_distinct_dirs_do_not_match(tmp_path):
    a = tmp_path / "a"; a.mkdir()
    b = tmp_path / "b"; b.mkdir()
    assert same_physical_dir(str(a), str(b)) is False


def test_case_variant_is_one_physical_dir(tmp_path):
    """On a case-insensitive filesystem (default macOS APFS, Windows), two cases of a
    name are the SAME physical directory — the invariant `same_physical_dir` must see.

    We deliberately do NOT assert anything about the intermediate `normalize_root`
    strings: whether they differ is environment-dependent (macOS APFS keeps the two
    spellings distinct; on the Windows machine we tested, `Path.resolve()` had already
    collapsed both to one on-disk casing). Asserting they differ was a macOS-specific
    assumption that failed on Windows. The property under test is physical identity."""
    if not _fs_is_case_insensitive(tmp_path):
        pytest.skip("filesystem is case-sensitive; case variants are genuinely distinct dirs")
    lower = tmp_path / "claims"
    lower.mkdir()
    upper_str = str(tmp_path / "CLAIMS")
    assert same_physical_dir(str(lower), upper_str)     # same physical dir, however the strings resolve
