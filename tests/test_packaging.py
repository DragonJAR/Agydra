"""Distribution contract: sdist/wheel members and installed-package behavior.

Builds both artifacts with stdlib setuptools from a temporary copy of the
project (no network, no install, no host paths) and exercises the wheel
contents exactly as a package installation would see them.
"""
from __future__ import annotations

import os
import re
import shutil
import site
import subprocess
import sys
import tarfile
import tempfile
import unittest
import unittest.mock
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IGNORED = shutil.ignore_patterns(
    ".git", ".venv", "venv", "__pycache__", "*.egg-info", "build", "dist",
    ".pytest_cache", ".atl", "*.pyc",
)
DOC_FILES = ("LICENSE", "README.md", "README.es.md", "AGENTS.md", "logo.png")

BUILD_SNIPPET = (
    "from setuptools import build_meta as b; "
    "print(b.build_sdist('dist')); print(b.build_wheel('dist'))"
)


def _py_module_list(pyproject: Path) -> list:
    text = pyproject.read_text(encoding="utf-8")
    block = re.search(r"py-modules\s*=\s*\[(.*?)\]", text, re.S).group(1)
    return re.findall(r'"([^"]+)"', block)


def _py_modules(pyproject: Path) -> set:
    return set(_py_module_list(pyproject))


def _clean_env(home: Path, **extra) -> dict:
    env = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "TEMP", "TMP", "TMPDIR")
        if key in os.environ
    }
    env.update(
        HOME=str(home), USERPROFILE=str(home), AGYDRA_REAL_HOME=str(home),
        XDG_CONFIG_HOME=str(home / "c"), XDG_DATA_HOME=str(home / "d"),
        LOCALAPPDATA=str(home / "l"), AGYDRA_HOME=str(home / "a"),
        AGYDRA_NO_KEYCHAIN="1", NO_COLOR="1", PYTHONDONTWRITEBYTECODE="1",
        PYTHONUSERBASE=site.getuserbase(),
    )
    env.update(extra)
    return env


MIN_SETUPTOOLS = 77


def _build_tools_problem():
    try:
        import setuptools
    except ImportError:
        return "setuptools is not available"
    major = re.match(r"\d+", setuptools.__version__)
    if major is None or int(major.group()) < MIN_SETUPTOOLS:
        return f"setuptools>={MIN_SETUPTOOLS} is required (found {setuptools.__version__})"
    return None


class TestDistributionArtifacts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        reason = _build_tools_problem()
        if reason:
            if os.environ.get("CI"):
                raise AssertionError(f"{reason}; the CI job must install it")
            raise unittest.SkipTest(reason)
        cls._tmp = tempfile.TemporaryDirectory(prefix="agydra-packaging-")
        base = Path(cls._tmp.name)
        cls.project = base / "project"
        shutil.copytree(ROOT, cls.project, ignore=IGNORED)
        done = subprocess.run(
            [sys.executable, "-c", BUILD_SNIPPET], cwd=cls.project,
            capture_output=True, text=True, timeout=300,
            env=_clean_env(base / "home"),
        )
        if done.returncode != 0:
            if "No module named 'wheel'" in done.stderr:
                raise unittest.SkipTest("wheel support is not available")
            raise AssertionError(f"build failed:\n{done.stdout}\n{done.stderr}")
        cls.build_output = done.stdout + done.stderr
        dist = cls.project / "dist"
        cls.sdist = next(dist.glob("*.tar.gz"))
        cls.wheel = next(dist.glob("*.whl"))
        cls.site = base / "site-packages"
        with zipfile.ZipFile(cls.wheel) as archive:
            cls.wheel_names = archive.namelist()
            archive.extractall(cls.site)
        with tarfile.open(cls.sdist) as archive:
            cls.sdist_names = [m.name for m in archive.getmembers() if m.isfile()]
        cls.sdist_root = base / "sdist"
        with tarfile.open(cls.sdist) as archive:
            archive.extractall(cls.sdist_root)
        cls.home = base / "home"
        cls.home.mkdir()

    @classmethod
    def tearDownClass(cls):
        cls._tmp.cleanup()

    def test_wheel_is_the_flat_runtime_modules_and_nothing_else(self):
        expected = {f"{name}.py" for name in _py_modules(ROOT / "pyproject.toml")}
        self.assertEqual(expected, {p.name for p in ROOT.glob("*.py")})
        listed = _py_module_list(ROOT / "pyproject.toml")
        self.assertEqual(len(listed), len(expected), "py-modules lists a module twice")
        self.assertTrue(expected)
        self.assertIn("claude_usage.py", expected)
        modules = {name for name in self.wheel_names if "/" not in name}
        self.assertEqual(modules, expected)
        for name in self.wheel_names:
            if "/" in name:
                self.assertIn(".dist-info/", name)
            self.assertNotRegex(name, r"(?i)test|conftest|\.log$|\.env|auth\.json|\.pyc$")

    def test_wheel_metadata_has_no_runtime_dependencies_and_one_console_script(self):
        dist_info = next(self.site.glob("*.dist-info"))
        metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
        self.assertNotIn("Requires-Dist", metadata)
        self.assertIn("Requires-Python: >=3.9", metadata)
        entry = (dist_info / "entry_points.txt").read_text(encoding="utf-8")
        self.assertEqual(entry.split(), ["[console_scripts]", "agydra", "=", "cli:main"])

    def test_wheel_metadata_uses_an_spdx_license_and_declares_supported_pythons(self):
        dist_info = next(self.site.glob("*.dist-info"))
        metadata = (dist_info / "METADATA").read_text(encoding="utf-8")
        self.assertIn("License-Expression: MIT", metadata)
        self.assertIn("License-File: LICENSE", metadata)
        self.assertNotRegex(metadata, r"(?m)^(License: |Classifier: License ::)")
        for minor in range(9, 15):
            self.assertIn(f"Classifier: Programming Language :: Python :: 3.{minor}", metadata)

    def test_build_emits_no_deprecation_warning_for_project_metadata(self):
        self.assertNotIn("SetuptoolsDeprecationWarning", self.build_output)
        self.assertNotIn("deprecated", self.build_output.lower())

    def test_sdist_ships_tests_conftest_and_docs_without_runtime_extras(self):
        names = {name.split("/", 1)[1] for name in self.sdist_names}
        for expected in (*DOC_FILES, "pyproject.toml", "MANIFEST.in", "tests/conftest.py"):
            self.assertIn(expected, names)
        for path in ROOT.joinpath("tests").glob("*.py"):
            self.assertIn(f"tests/{path.name}", names)
        for name in names:
            self.assertNotRegex(name, r"__pycache__|\.pyc$|\.log$|(^|/)\.venv|auth\.json|\.env$")

    def test_extracted_sdist_collects_tests_without_the_repository_on_the_path(self):
        try:
            __import__("pytest")
        except ImportError:
            self.skipTest("pytest is not available")
        root = next(self.sdist_root.iterdir())
        done = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider",
             "tests", "--ignore=tests/test_packaging.py"],
            cwd=root, capture_output=True, text=True, timeout=300,
            env=_clean_env(self.home),
        )
        self.assertEqual(done.returncode, 0, done.stdout[-2000:] + done.stderr[-2000:])
        self.assertNotIn("error", done.stdout.lower().split("collected")[-1])

    def _run_installed(self, *argv):
        return subprocess.run(
            [sys.executable, *argv], cwd=self.home, capture_output=True, text=True,
            timeout=120, env=_clean_env(self.home, PYTHONPATH=str(self.site)),
        )

    def test_installed_module_entry_enters_the_cli_without_building_a_venv(self):
        done = self._run_installed("-m", "agydra", "--version")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith("agydra "))
        self.assertFalse((self.site / ".venv").exists())
        self.assertFalse((self.home / ".local").exists())

    def test_installed_setup_reports_pip_management_and_writes_nothing(self):
        before = sorted(p.name for p in self.site.iterdir())
        entry = "import sys; from cli import main; sys.exit(main())"
        for flags in ((), ("--force",)):
            done = self._run_installed("-c", entry, "setup", *flags)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn("pip/pipx manages this installation", done.stdout)
        dry = self._run_installed("-c", entry, "setup", "-n")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertNotIn("[--] venv", dry.stdout)
        self.assertIn("shim: pip-managed", dry.stdout)
        self.assertEqual(sorted(p.name for p in self.site.iterdir()), before)
        self.assertFalse((self.site / ".venv").exists())
        self.assertFalse((self.home / ".local").exists())


class TestBuildToolsPolicy(unittest.TestCase):
    def test_missing_or_old_setuptools_is_reported(self):
        import types

        for version, expected in (("76.1.0", "setuptools>=77"), ("77.0.0", None), ("82.0.1", None)):
            with self.subTest(version=version):
                fake = types.SimpleNamespace(__version__=version)
                with unittest.mock.patch.dict(sys.modules, {"setuptools": fake}):
                    problem = _build_tools_problem()
                if expected is None:
                    self.assertIsNone(problem)
                else:
                    self.assertIn(expected, problem)
        with unittest.mock.patch.dict(sys.modules, {"setuptools": None}):
            self.assertEqual(_build_tools_problem(), "setuptools is not available")

    def test_ci_fails_instead_of_silently_skipping_the_packaging_contract(self):
        with unittest.mock.patch.dict(sys.modules, {"setuptools": None}):
            with unittest.mock.patch.dict(os.environ, {"CI": "true"}):
                with self.assertRaisesRegex(AssertionError, "CI job must install it"):
                    TestDistributionArtifacts.setUpClass()
            with unittest.mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("CI", None)
                with self.assertRaises(unittest.SkipTest):
                    TestDistributionArtifacts.setUpClass()


class TestWorkflowInstallsBuildTools(unittest.TestCase):
    def test_every_job_installs_setuptools_before_the_suite(self):
        text = (ROOT / ".github" / "workflows" / "tests.yml").read_text(encoding="utf-8")
        install = text.index('pip install --disable-pip-version-check "setuptools>=77"')
        suite = text.index("python -m unittest discover -s tests -p \"test_*.py\"")
        self.assertLess(install, suite)
        self.assertEqual(text.count("os: "), 3)


if __name__ == "__main__":
    unittest.main()
