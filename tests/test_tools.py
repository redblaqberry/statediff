"""tools/capture_baselines.py: the fixture promotion swap, the upstream
checkout lookup, and the JSON-RPC validation the fixtures README calls a
validated conversation. All are exercised without a SiloBench checkout, since
the failure modes under test are filesystem, configuration, and protocol
ones."""

import json
from pathlib import Path

import pytest

from tools import capture_baselines


def _staging(tmp_path: Path) -> Path:
    staging = tmp_path / "staging"
    (staging / "payment").mkdir(parents=True)
    (staging / "payment" / "before-snapshot.json").write_text("new", encoding="utf-8")
    return staging


def _live_baseline(tmp_path: Path, monkeypatch) -> Path:
    fixtures = tmp_path / "fixtures" / "baseline"
    (fixtures / "payment").mkdir(parents=True)
    (fixtures / "payment" / "before-snapshot.json").write_text("old", encoding="utf-8")
    monkeypatch.setattr(capture_baselines, "FIXTURES", fixtures)
    return fixtures


def _break_the_swap(monkeypatch) -> None:
    """Fail the incoming -> baseline rename, the second half of the swap. That
    is the window where the checkout has no fixtures/baseline at all."""
    real_rename = Path.rename

    def rename(self, target):
        if self.name == "baseline.incoming":
            raise OSError("simulated interruption")
        return real_rename(self, target)

    monkeypatch.setattr(Path, "rename", rename)


def test_promote_swaps_the_new_tree_in(tmp_path, monkeypatch):
    fixtures = _live_baseline(tmp_path, monkeypatch)
    capture_baselines.promote(_staging(tmp_path), {"format": "test"})
    assert (fixtures / "payment" / "before-snapshot.json").read_text(encoding="utf-8") == "new"
    assert json.loads((fixtures / "capture-provenance.json").read_text(encoding="utf-8")) == {"format": "test"}
    for scratch in ("baseline.incoming", "baseline.outgoing"):
        assert not (fixtures.parent / scratch).exists()


def test_promote_restores_the_baseline_when_the_swap_fails(tmp_path, monkeypatch):
    fixtures = _live_baseline(tmp_path, monkeypatch)
    _break_the_swap(monkeypatch)
    with pytest.raises(OSError):
        capture_baselines.promote(_staging(tmp_path), {"format": "test"})
    assert (fixtures / "payment" / "before-snapshot.json").read_text(encoding="utf-8") == "old"


def test_promote_recovers_a_baseline_stranded_by_an_earlier_interruption(tmp_path, monkeypatch):
    fixtures = _live_baseline(tmp_path, monkeypatch)
    # Exactly what a kill between the two renames leaves behind: no baseline,
    # the only copy parked under baseline.outgoing. Without the recovery step
    # the leftover cleanup deletes it and the checkout loses its fixtures.
    fixtures.rename(fixtures.parent / "baseline.outgoing")
    _break_the_swap(monkeypatch)
    with pytest.raises(OSError):
        capture_baselines.promote(_staging(tmp_path), {"format": "test"})
    assert (fixtures / "payment" / "before-snapshot.json").read_text(encoding="utf-8") == "old"


def _fake_checkout(path: Path) -> Path:
    marker = path / capture_baselines.SILOBENCH_MARKER
    marker.parent.mkdir(parents=True)
    marker.write_text("", encoding="utf-8")
    return path


def test_find_silobench_prefers_the_explicit_path_over_everything(tmp_path, monkeypatch):
    checkout = _fake_checkout(tmp_path / "anywhere" / "silobench")
    monkeypatch.setenv("SILOBENCH_REPO", str(tmp_path / "not-a-checkout"))
    assert capture_baselines.find_silobench(str(checkout)) == checkout.resolve()


def test_find_silobench_uses_the_env_var_then_the_sibling_layout(tmp_path, monkeypatch):
    sibling = _fake_checkout(tmp_path / "workspace" / "02-silobench")
    monkeypatch.setattr(capture_baselines, "REPO_ROOT", tmp_path / "workspace" / "03-statediff")
    monkeypatch.setenv("SILOBENCH_REPO", str(sibling))
    assert capture_baselines.find_silobench() == sibling.resolve()
    # The sibling probe is the documented fallback and still works unchanged.
    monkeypatch.delenv("SILOBENCH_REPO")
    assert capture_baselines.find_silobench() == sibling.resolve()


def test_find_silobench_rejects_a_named_path_that_is_not_a_checkout(tmp_path, monkeypatch):
    # A named location is never silently skipped in favour of a sibling that
    # happens to exist: the operator asked for this one.
    _fake_checkout(tmp_path / "workspace" / "silobench")
    monkeypatch.setattr(capture_baselines, "REPO_ROOT", tmp_path / "workspace" / "03-statediff")
    monkeypatch.delenv("SILOBENCH_REPO", raising=False)
    with pytest.raises(SystemExit) as failure:
        capture_baselines.find_silobench(str(tmp_path / "typo"))
    assert "not a SiloBench checkout" in str(failure.value)


def test_find_silobench_does_not_treat_an_empty_named_path_as_no_path(tmp_path, monkeypatch):
    # PRESENCE decides, not truthiness. Read for truthiness, `--silobench ""`
    # counted as no flag at all and the run fell through to the environment or
    # to whichever sibling happened to exist, capturing committed baselines
    # from a checkout nobody named while the provenance file recorded that
    # checkout's commit as though it had been chosen.
    sibling = _fake_checkout(tmp_path / "workspace" / "02-silobench")
    monkeypatch.setattr(capture_baselines, "REPO_ROOT", tmp_path / "workspace" / "03-statediff")
    monkeypatch.setenv("SILOBENCH_REPO", str(sibling))
    with pytest.raises(SystemExit) as failure:
        capture_baselines.find_silobench("")
    assert "path is empty" in str(failure.value)

    # An empty environment variable is the same misconfiguration one layer out.
    monkeypatch.setenv("SILOBENCH_REPO", "   ")
    with pytest.raises(SystemExit):
        capture_baselines.find_silobench()

    # Omitting both is the documented fallback and still finds the sibling.
    monkeypatch.delenv("SILOBENCH_REPO")
    assert capture_baselines.find_silobench() == sibling.resolve()


# JSON-RPC validation --------------------------------------------------------

def test_response_id_matching_is_type_strict():
    # Python's True == 1 and 1.0 == 1, so plain equality let a boolean or float
    # id answer a request that used an integer one.
    assert capture_baselines.is_response_to({"jsonrpc": "2.0", "id": 2, "result": {}}, 2)
    for msg in (
        {"jsonrpc": "2.0", "id": True, "result": {}},
        {"jsonrpc": "2.0", "id": 1.0, "result": {}},
        {"jsonrpc": "2.0", "id": "1", "result": {}},
        {"jsonrpc": "2.0", "id": 2, "result": {}},
        {"jsonrpc": "2.0", "method": "notifications/message"},
        "not an object",
    ):
        assert not capture_baselines.is_response_to(msg, 1)


def test_response_result_requires_framing_and_an_object_result():
    good = {"jsonrpc": "2.0", "id": 1, "result": {"ok": 1}}
    assert capture_baselines.response_result("erp", good, 1) == {"ok": 1}
    for msg in (
        {"id": 1, "result": {}},
        {"jsonrpc": "1.0", "id": 1, "result": {}},
        {"jsonrpc": "2.0", "id": 1, "result": "ok"},
        {"jsonrpc": "2.0", "id": 1, "result": None},
        {"jsonrpc": "2.0", "id": 1},
    ):
        with pytest.raises(SystemExit):
            capture_baselines.response_result("erp", msg, 1)


def test_initialize_result_is_held_to_the_protocol_version_it_records():
    good = {
        "protocolVersion": capture_baselines.PROTOCOL_VERSION,
        "capabilities": {},
        "serverInfo": {"name": "silobench-erp", "version": "0.1.0"},
    }
    capture_baselines.validate_initialize_result("erp", good)
    # Every one of these is truthy, which used to be the whole check, and each
    # describes a session the capture provenance would misreport.
    for bad in (
        {**good, "protocolVersion": "1999-01-01"},
        {**good, "protocolVersion": 42},
        {**good, "serverInfo": "silobench-erp"},
        {**good, "serverInfo": {"version": "0.1.0"}},
        {**good, "capabilities": "yes"},
    ):
        with pytest.raises(SystemExit):
            capture_baselines.validate_initialize_result("erp", bad)


def test_tool_result_is_error_must_be_a_real_boolean():
    capture_baselines.validate_tool_result("erp_release_payment", {"content": []})
    capture_baselines.validate_tool_result("erp_release_payment", {"isError": False})
    # 0 read as success and "false" read as an error are both accidents of
    # truthiness, not readings of what the server said.
    for bad in ({"isError": 0}, {"isError": "false"}, {"isError": None}, {"isError": True}):
        with pytest.raises(SystemExit):
            capture_baselines.validate_tool_result("erp_release_payment", bad)


def test_the_capture_procedure_is_documented_as_it_actually_behaves():
    # `isError` is optional in the MCP tool-result schema and its absence is
    # defined as false, so the omission above is the protocol's own reading and
    # demanding the key would reject conformant servers. The fixtures README
    # claimed a capture "must carry a real boolean isError", which no code
    # enforced and none should: a procedure document that overstates what was
    # checked is the same defect as a verdict that does, since the fixtures'
    # provenance is exactly what that file is evidence for.
    doc = (Path(__file__).resolve().parents[1] / "fixtures" / "README.md").read_text(encoding="utf-8")
    assert "must carry a real boolean `isError`" not in doc
    assert "the field is OPTIONAL and its absence means" in doc
    capture_baselines.validate_tool_result("erp_release_payment", {"content": []})
