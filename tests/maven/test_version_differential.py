"""Differential test: the Python port against Maven's own ComparableVersion.

Needs ``java`` and maven-artifact-3.9.11.jar. Set GIML_MAVEN_ARTIFACT_JAR to the jar, or it is
looked for under ``$MAVEN_HOME/lib`` and the ``mvn`` on PATH. Skipped when either is missing.
Run with ``pytest -m slow``.
"""

import os
import random
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from giml.maven.version import ComparableVersion

pytestmark = pytest.mark.slow

SEED = 20260924
SAMPLES = 3000
TOKENS = ["0", "1", "2", "9", "10", "00", "123", "1234567890", "12345678901234567890",
          "alpha", "a", "beta", "b", "m", "milestone", "rc", "cr", "snapshot", "SNAPSHOT",
          "ga", "final", "RELEASE", "sp", "x", "abc", "Final"]  # fmt: skip
SEPARATORS = [".", "-", ""]


def find_jar() -> Path | None:
    explicit = os.environ.get("GIML_MAVEN_ARTIFACT_JAR")
    if explicit:
        return Path(explicit)
    homes = [os.environ.get("MAVEN_HOME")]
    mvn = shutil.which("mvn")
    if mvn:
        homes.append(str(Path(mvn).resolve().parent.parent))
    for home in filter(None, homes):
        jar = Path(home) / "lib" / "maven-artifact-3.9.11.jar"
        if jar.is_file():
            return jar
    return None


def random_versions(rng: random.Random, count: int) -> list[str]:
    versions = []
    for _ in range(count):
        parts = [rng.choice(TOKENS)]
        for _ in range(rng.randint(0, 4)):
            parts.append(rng.choice(SEPARATORS))
            parts.append(rng.choice(TOKENS))
        versions.append("".join(parts))
    return versions


def test_port_matches_maven_artifact_jar():
    jar = find_jar()
    if jar is None or shutil.which("java") is None:
        pytest.skip("java or maven-artifact-3.9.11.jar not available")
    versions = random_versions(random.Random(SEED), SAMPLES)
    out = subprocess.run(
        ["java", "-cp", str(jar), "org.apache.maven.artifact.versioning.ComparableVersion", *versions],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout  # fmt: skip

    canonical = re.findall(r"^\d+\. (.*) -> (.*); tokens: ", out, re.MULTILINE)
    comparisons = re.findall(r"^   (\S+) (==|<|>) (\S+)$", out, re.MULTILINE)
    assert len(canonical) == SAMPLES and len(comparisons) == SAMPLES - 1

    for version, expected in canonical:
        assert ComparableVersion(version).canonical == expected, version
    symbol = {-1: "<", 0: "==", 1: ">"}
    for left, op, right in comparisons:
        result = ComparableVersion(left).compare_to(ComparableVersion(right))
        assert symbol[(result > 0) - (result < 0)] == op, f"{left} {op} {right}"
