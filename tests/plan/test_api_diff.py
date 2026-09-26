from pathlib import Path

from giml.plan.api_diff import ApiChecker, Break, parse_japicmp, project_sources, used_breaks

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "japicmp"


def report(name):
    return (FIXTURES / name).read_text()


def test_a_removed_method_is_a_break_of_that_member():
    assert parse_japicmp(report("method-removed.xml")) == (Break("fx.Lib", "hello", "METHOD_REMOVED"),)


def test_a_removed_class_and_a_removed_field_are_both_reported():
    found = parse_japicmp(report("class-and-field-removed.xml"))
    assert Break("fx.Gone", None, "CLASS_REMOVED") in found and Break("fx.Lib", "f", "FIELD_REMOVED") in found


def test_a_report_without_incompatible_changes_has_no_breaks():
    assert parse_japicmp('<japicmp><classes><class binaryCompatible="true" sourceCompatible="true" fullyQualifiedName="a.B" changeStatus="UNCHANGED"/></classes></japicmp>') == ()


def test_a_break_is_used_when_the_class_and_the_member_are_named_as_whole_words():
    lib = Break("fx.Lib", "hello", "METHOD_REMOVED")
    assert used_breaks([lib], ["import fx.Lib; class A { String s = Lib.hello(); }"]) == (lib,)
    assert used_breaks([lib], ["class A { String s = Lib.other(); }"]) == ()  # the class but not the member
    assert used_breaks([lib], ["class A { String s = hello(); }"]) == ()  # the member but not the class
    assert used_breaks([lib], ["class A { Library l; String hello_world; }"]) == ()  # only as part of longer words


def test_a_class_level_break_only_needs_the_class_and_a_nested_class_counts_as_its_outer_one():
    assert used_breaks([Break("fx.Gone", None, "CLASS_REMOVED")], ["new Gone()"]) != ()
    assert used_breaks([Break("fx.Outer$Inner", "m", "METHOD_REMOVED")], ["Outer o; o.m();"]) != ()


def test_project_sources_are_read_without_build_output(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "A.java").write_text("class A {}")
    (tmp_path / "target").mkdir()
    (tmp_path / "target" / "Gen.java").write_text("class Gen {}")
    assert project_sources(tmp_path) == ["class A {}"]


def test_the_checker_reports_used_breaks_once_per_transition_and_skips_nothing_when_a_jar_is_missing():
    calls = []

    def jar(coordinate, version):
        calls.append(("jar", version))
        return None if version == "9" else Path(f"/{version}.jar")

    checker = ApiChecker(["Lib.hello()"], jar, lambda old, new: report("method-removed.xml"))
    assert checker.check("fx:lib", "1.0", "2.0") == ("fx.Lib.hello (METHOD_REMOVED)",)
    assert checker.check("fx:lib", "1.0", "2.0") == ("fx.Lib.hello (METHOD_REMOVED)",) and calls.count(("jar", "2.0")) == 1
    assert checker.check("fx:lib", "1.0", "9") == ()  # no jar for it: no evidence, so nothing is skipped
    assert ApiChecker(["Lib.hello()"], jar, lambda old, new: None).check("fx:lib", "1.0", "2.0") == ()
