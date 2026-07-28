from pcb_gate.report import Report


def test_pass_when_checks_ran_clean(capsys):
    report = Report(tool="t", project="p")
    report.check("did a thing")
    assert report.ok
    code = report.summarize()
    assert code == 0
    assert "PASS" in capsys.readouterr().out


def test_fail_on_violation(capsys):
    report = Report(tool="t", project="p")
    report.check("did a thing")
    report.fail("some_code", "went wrong")
    assert not report.ok
    code = report.summarize()
    assert code == 1
    assert "FAIL" in capsys.readouterr().err


def test_benign_skip_does_not_break_ok():
    report = Report(tool="t", project="p")
    report.check("determined this doesn't apply")
    report.skip("not applicable on this board")
    assert report.ok
    assert report.skipped == ["not applicable on this board"]
    assert report.skipped_blocking == []


def test_blocking_skip_makes_report_not_ok():
    report = Report(tool="t", project="p")
    report.check("tried to run the check")
    report.skip("could not load a required file", blocking=True)
    assert not report.ok
    assert report.skipped_blocking == ["could not load a required file"]
    assert report.skipped == ["could not load a required file"]


def test_blocking_skip_fails_summarize(capsys):
    report = Report(tool="t", project="p")
    report.check("tried to run the check")
    report.skip("could not load a required file", blocking=True)
    code = report.summarize()
    assert code == 1
    assert "FAIL" in capsys.readouterr().err


def test_zero_checks_is_inconclusive_not_pass(capsys):
    """A report that never called check() must never print PASS, even if `ok` is technically True."""
    report = Report(tool="t", project="p")
    assert report.ok  # no violations, no blocking skips - but nothing ran either
    code = report.summarize()
    assert code == 1
    err = capsys.readouterr().err
    assert "INCONCLUSIVE" in err
    assert "PASS" not in err


def test_zero_checks_with_only_a_benign_skip_is_still_inconclusive():
    report = Report(tool="t", project="p")
    report.skip("nothing to check")
    assert report.checked == []
    code = report.summarize()
    assert code == 1


def test_to_json_includes_skipped_blocking():
    report = Report(tool="t", project="p")
    report.check("ran a check")
    report.skip("blocking one", blocking=True)
    report.skip("benign one")
    data = report.to_json()
    assert data["skipped_blocking"] == ["blocking one"]
    assert data["skipped"] == ["blocking one", "benign one"]
    assert data["ok"] is False
