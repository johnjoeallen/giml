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


class CopyMaven:
    """Answers dependency:copy by writing the artifact's jar into the output directory (or failing)."""

    def __init__(self, fail=()):
        self.fail, self.calls = set(fail), []

    def __call__(self, project, args, log, timeout, env):
        from giml.maven.runner import MavenResult

        artifact = next(a.split("=", 1)[1] for a in args if a.startswith("-Dartifact="))
        out = Path(next(a.split("=", 1)[1] for a in args if a.startswith("-DoutputDirectory=")))
        self.calls.append((artifact, out))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("copy\n")
        ok = not any(f in artifact for f in self.fail)
        if ok:
            group, name, version, *rest = artifact.split(":")
            suffix = f"-{rest[-1]}" if len(rest) > 1 else ""
            (out / f"{name}-{version}{suffix}.jar").write_bytes(b"jar")
        return MavenResult(tuple(args), 0 if ok else 1, 0.1, log, False)


class FakeJava:
    def __init__(self, xml=None, code=0):
        self.xml, self.code, self.calls = xml, code, []

    def __call__(self, args, timeout, env=None):
        import subprocess

        self.calls.append(args)
        if self.xml is not None:
            Path(args[args.index("--xml-file") + 1]).write_text(self.xml)
        return subprocess.CompletedProcess(args, self.code, "", "")


def tools(tmp_path, maven, java):
    from giml.plan.api_diff import JapicmpTools

    return JapicmpTools(tmp_path / "tools", tmp_path / "proj", maven, java, {}, tmp_path / "logs", 60)


def test_jars_are_fetched_once_and_kept_in_the_tools_directory(tmp_path):
    maven = CopyMaven()
    api = tools(tmp_path, maven, FakeJava())
    first = api.jar("fx:lib", "1.0.0")
    assert first == tmp_path / "tools" / "jars" / "fx" / "lib" / "1.0.0" / "lib-1.0.0.jar" and first.is_file()
    assert api.jar("fx:lib", "1.0.0") == first and len(maven.calls) == 1 and maven.calls[0][0] == "fx:lib:1.0.0:jar"


def test_a_jar_that_cannot_be_fetched_is_none(tmp_path):
    assert tools(tmp_path, CopyMaven(fail={"lib"}), FakeJava()).jar("fx:lib", "9") is None


def test_japicmp_is_fetched_once_and_run_on_the_two_jars(tmp_path):
    maven, java = CopyMaven(), FakeJava(xml="<japicmp/>")
    api = tools(tmp_path, maven, java)
    old, new = api.jar("fx:lib", "1.0.0"), api.jar("fx:lib", "1.1.0")
    assert api.compare(old, new) == "<japicmp/>" and api.compare(old, new) == "<japicmp/>"
    assert [c[0] for c in maven.calls].count("com.github.siom79.japicmp:japicmp:0.26.2:jar:jar-with-dependencies") == 1
    args = java.calls[0]
    assert args[0] == "-jar" and args[2:6] == ["--old", str(old), "--new", str(new)] and "--only-incompatible" in args


def test_a_failing_japicmp_or_a_missing_tool_gives_no_evidence(tmp_path):
    api = tools(tmp_path, CopyMaven(), FakeJava(xml="<x/>", code=1))
    assert api.compare(api.jar("fx:lib", "1"), api.jar("fx:lib", "2")) is None
    assert tools(tmp_path / "b", CopyMaven(fail={"japicmp"}), FakeJava(xml="<x/>")).compare(Path("/a.jar"), Path("/b.jar")) is None


def test_a_class_whose_name_starts_with_a_dollar_does_not_match_every_source():
    internal = Break("com.google.gson.internal.$Gson$Preconditions", None, "CLASS_REMOVED")
    assert used_breaks([internal], ["class A { Gson g; }"]) == ()  # found on real gson: it once matched everything
    assert used_breaks([internal], ["Object o = $Gson$Preconditions.checkNotNull(x);"]) == (internal,)
