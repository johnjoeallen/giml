import json
import shutil
from pathlib import Path

import pytest

from giml.core.model import Coordinate
from giml.maven.runner import MavenResult
from giml.maven.tree import Dependency, ResolutionError, TreeError, parse_tree, resolve_reactor

TREES = Path(__file__).resolve().parents[1] / "fixtures" / "trees"


def c(text: str) -> Coordinate:
    return Coordinate.parse(text)


def node(group="g", artifact="a", version="1", scope="compile", children=(), **extra) -> dict:
    return {"groupId": group, "artifactId": artifact, "version": version, "type": "jar", "scope": scope,
            "classifier": "", "optional": "false", "children": list(children), **extra}  # fmt: skip


def test_real_server_tree_is_flattened_with_the_path_to_each_dependency():
    tree = parse_tree((TREES / "redkite-server.json").read_text())
    assert (tree.coordinate, tree.version) == (c("com.redkite:red-kite-server"), "0.1.0-SNAPSHOT")
    by_coordinate = {d.coordinate: d for d in tree.dependencies}
    assert len(tree.dependencies) == 21 and len(by_coordinate) == 21
    h2 = by_coordinate[c("com.h2database:h2")]
    assert (h2.version, h2.scope, h2.type, h2.classifier, h2.optional, h2.via) == (
        "2.5.250", "runtime", "jar", "", False, ())  # fmt: skip
    assert h2.direct
    javassist = by_coordinate[c("org.javassist:javassist")]
    assert javassist.via == (c("org.thymeleaf:thymeleaf"), c("ognl:ognl")) and not javassist.direct
    # Maven's own order: a dependency comes before its children, siblings in listed order.
    order = [str(d.coordinate) for d in tree.dependencies]
    assert order.index("org.thymeleaf:thymeleaf") < order.index("ognl:ognl") < order.index("org.javassist:javassist")


def test_module_without_dependencies():
    tree = parse_tree(json.dumps(node("com.x", "empty", "1.0")))
    assert tree.dependencies == ()


def test_optional_classifier_and_missing_scope():
    document = node("m", "root", "1", children=[
        node("o", "opt", "2", optional="true"),
        node("t", "tests", "3", scope="", classifier="tests"),
    ])  # fmt: skip
    dependencies = parse_tree(json.dumps(document)).dependencies
    assert [(d.coordinate, d.optional, d.classifier, d.scope) for d in dependencies] == [
        (c("o:opt"), True, "", "compile"), (c("t:tests"), False, "tests", "")]  # fmt: skip


def test_the_same_artifact_can_appear_at_several_places():
    document = node(children=[
        node("x", "one", children=[node("s", "shared", "1")]),
        node("y", "two", children=[node("s", "shared", "2")]),
    ])  # fmt: skip
    shared = [d for d in parse_tree(json.dumps(document)).dependencies if d.coordinate == c("s:shared")]
    assert [(d.version, d.via) for d in shared] == [("1", (c("x:one"),)), ("2", (c("y:two"),))]


@pytest.mark.parametrize(("text", "error"), [
    ("{", r"not valid JSON"),
    ("[]", r"expected an object"),
    (json.dumps({"groupId": "g"}), r"missing artifactId"),
    (json.dumps(node(version=1)), r"version: expected a string"),
    (json.dumps(node(children=[node(artifact="")])), r"invalid Maven coordinate"),
    (json.dumps({**node(), "children": "x"}), r"children: expected a list"),
    (json.dumps(node(children=["x"])), r"expected an object"),
    (json.dumps(node(children=[node(optional="maybe")])), r"optional: expected true or false"),
])  # fmt: skip
def test_malformed_trees_are_rejected(text, error):
    with pytest.raises(TreeError, match=error):
        parse_tree(text)


class FakeMaven:
    """Writes each module's tree file the way the dependency plugin does."""

    def __init__(self, trees: dict[str, str], succeed=True):
        self.trees, self.succeed, self.calls = trees, succeed, []

    def __call__(self, project, args, log, timeout, env):
        self.calls.append((project, args, timeout, env))
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("fake mvn\n")
        if self.succeed:
            for module, text in self.trees.items():
                target = project / module / "target"
                target.mkdir(parents=True, exist_ok=True)
                (target / "giml-tree.json").write_text(text)
        return MavenResult(tuple(args), 0 if self.succeed else 1, 0.1, log, False)


def reactor(tmp_path) -> tuple[Path, list[Path]]:
    (tmp_path / "core").mkdir()
    return tmp_path, [tmp_path / "pom.xml", tmp_path / "core" / "pom.xml"]


def test_resolve_reactor_reads_every_modules_tree(tmp_path):
    project, poms = reactor(tmp_path)
    maven = FakeMaven({".": json.dumps(node("com.x", "parent", "1")),
                       "core": (TREES / "redkite-core.json").read_text()})  # fmt: skip
    trees = resolve_reactor(project, poms, tmp_path / "logs" / "tree.log", 60, maven, {"JAVA_HOME": "/j"})
    assert [str(t.coordinate) for t in trees] == ["com.x:parent", "com.redkite:red-kite-core"]
    assert len(trees[1].dependencies) == 8
    (_, args, timeout, env), = maven.calls
    assert args[0] == "org.apache.maven.plugins:maven-dependency-plugin:3.11.0:tree"
    assert "-DoutputType=json" in args and "-DoutputFile=target/giml-tree.json" in args
    assert "-Denforcer.skip=true" in args and timeout == 60 and env == {"JAVA_HOME": "/j"}


def test_resolve_reactor_failure_names_the_log(tmp_path):
    project, poms = reactor(tmp_path)
    with pytest.raises(ResolutionError, match=r"dependency resolution failed; see .*tree\.log"):
        resolve_reactor(project, poms, tmp_path / "tree.log", 60, FakeMaven({}, succeed=False), None)


def test_resolve_reactor_missing_tree_file_names_the_module(tmp_path):
    project, poms = reactor(tmp_path)
    with pytest.raises(ResolutionError, match=r"no dependency tree written for .*core"):
        resolve_reactor(project, poms, tmp_path / "tree.log", 60, FakeMaven({".": json.dumps(node())}), None)


def test_resolve_reactor_bad_tree_file_names_the_module(tmp_path):
    project, poms = reactor(tmp_path)
    maven = FakeMaven({".": json.dumps(node()), "core": "{"})
    with pytest.raises(ResolutionError, match=r"core.*giml-tree\.json: .*not valid JSON"):
        resolve_reactor(project, poms, tmp_path / "tree.log", 60, maven, None)


def test_stale_tree_files_are_not_trusted(tmp_path):
    # A tree left in target/ by an earlier run must not stand in for a failed plugin run.
    project, poms = reactor(tmp_path)
    for module in (".", "core"):
        (project / module / "target").mkdir(parents=True, exist_ok=True)
        shutil.copy(TREES / "redkite-core.json", project / module / "target" / "giml-tree.json")
    with pytest.raises(ResolutionError, match="dependency resolution failed"):
        resolve_reactor(project, poms, tmp_path / "tree.log", 60, FakeMaven({}, succeed=False), None)
    with pytest.raises(ResolutionError, match="no dependency tree written"):
        resolve_reactor(project, poms, tmp_path / "tree.log", 60, FakeMaven({}), None)


def test_dependency_direct_property():
    base = dict(coordinate=c("a:b"), version="1", scope="compile", type="jar", classifier="", optional=False)
    assert Dependency(**base, via=()).direct and not Dependency(**base, via=(c("x:y"),)).direct
