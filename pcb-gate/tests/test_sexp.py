from pcb_gate import sexp


def test_parse_nested_and_quoted():
    text = '(zone (net "GND") (layer "F.Cu") (min_thickness 0.25))'
    (root,) = sexp.parse(text)
    assert sexp.tag(root) == "zone"
    assert sexp.text_of(sexp.child(root, "net")) == "GND"
    assert sexp.text_of(sexp.child(root, "layer")) == "F.Cu"
    assert sexp.child(root, "min_thickness")[1] == "0.25"


def test_children_vs_find_all():
    text = "(a (b 1) (c (b 2)))"
    (root,) = sexp.parse(text)
    assert len(list(sexp.children(root, "b"))) == 1
    assert len(list(sexp.find_all(root, "b"))) == 2


def test_round_trip_preserves_quoting():
    text = '(segment (layer "F.Cu") (width 0.2) (net "/SCL"))'
    (root,) = sexp.parse(text)
    dumped = sexp.dumps(root)
    (reparsed,) = sexp.parse(dumped)
    assert sexp.text_of(sexp.child(reparsed, "layer")) == "F.Cu"
    assert sexp.child(reparsed, "width")[1] == "0.2"
    assert sexp.text_of(sexp.child(reparsed, "net")) == "/SCL"


def test_unbalanced_parens_raise():
    import pytest

    with pytest.raises(sexp.SexpError):
        sexp.parse("(a (b)")
    with pytest.raises(sexp.SexpError):
        sexp.parse("(a))")


def test_multi_top_level_expressions_for_dru_style_files():
    text = '(version 1)\n\n(rule "x" (constraint assertion "false"))'
    top = sexp.parse(text)
    assert len(top) == 2
    assert sexp.tag(top[0]) == "version"
    assert sexp.tag(top[1]) == "rule"
