import _paths  # noqa: F401  (must come first: puts kit/scripts on sys.path)
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import common

ROOT = Path(__file__).resolve().parent.parent.parent
INSTALL = ROOT / "install.sh"
KIT = ROOT / "kit"
VERSION = (KIT / "VERSION").read_text(encoding="utf-8").strip()


def _env(**extra):
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_SYSTEM"] = os.devnull
    env.update(extra)
    return env


def snapshot(path):
    """Every entry under `path` (directories, symlinks and file bytes), for byte-identical comparisons."""
    found = {}
    for current, dirs, names in os.walk(path):
        for name in [*dirs, *names]:
            entry = Path(current) / name
            key = entry.relative_to(path).as_posix()
            if entry.is_symlink():
                found[key] = ("link", os.readlink(entry))
            elif entry.is_dir():
                found[key] = ("dir",)
            else:
                found[key] = ("file", entry.read_bytes())
    return found


class InstallCase(unittest.TestCase):
    shell = "sh"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name).resolve()

    def repo(self, name="target"):
        path = self.tmp / name
        path.mkdir()
        subprocess.run(["git", "init", "-q", str(path)], check=True, env=_env())
        subprocess.run(["git", "-C", str(path), "symbolic-ref", "HEAD", "refs/heads/main"], check=True, env=_env())
        return path

    def install(self, *args, script=INSTALL, cwd=None, env=None, shell=None):
        return subprocess.run([shell or self.shell, str(script), *map(str, args)], cwd=cwd or self.tmp,
                              capture_output=True, text=True, env=env or _env())

    def pk(self, target, *args):
        return subprocess.run([sys.executable, str(target / "kit" / "scripts" / "pk.py"), *args], cwd=target,
                              capture_output=True, text=True, env=_env())

    def assert_ok(self, result):
        self.assertEqual((result.returncode, result.stderr), (0, ""), result.stdout)

    def assert_refused(self, result, fragment):
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("error: ", result.stderr)
        self.assertIn(fragment, result.stderr)

    def leftovers(self, target):
        return sorted(p.name for p in Path(target).iterdir() if p.name.startswith((".kit.tmp.", ".kit.old.")))


class InstallTests(InstallCase):
    def test_install_fresh_copies_kit_and_inits_program(self):
        for preset in (None, "go", "node", "python"):
            with self.subTest(preset=preset):
                target = self.repo(f"t-{preset}")
                result = self.install(target, *(["--preset", preset] if preset else []))
                self.assert_ok(result)
                self.assertEqual(result.stdout.splitlines()[-1], f"installed program-kit {VERSION} into {target}")
                self.assertEqual((target / "kit" / "VERSION").read_text().strip(), VERSION)
                self.assertTrue((target / "kit" / "MANIFEST.sha256").is_file())
                self.assertTrue((target / "program" / "BRIEF.md").is_file())
                expected = KIT / "presets" / f"checks.{preset}.yaml" if preset else KIT / "templates" / "checks.yaml"
                self.assertEqual((target / "program" / "checks.yaml").read_bytes(), expected.read_bytes())
                self.assertEqual(self.leftovers(target), [])
                check = self.pk(target, "manifest", "check")
                self.assertEqual((check.returncode, check.stdout), (0, "manifest: clean\n"), check.stderr)
                staged = subprocess.run(["git", "-C", str(target), "diff", "--cached", "--name-only"],
                                        capture_output=True, text=True, env=_env())
                self.assertEqual(staged.stdout, "")

    def test_install_refuses_existing_kit_without_upgrade(self):
        target = self.repo()
        (target / "kit").mkdir()
        (target / "kit" / "mine.txt").write_text("keep\n")
        result = self.install(target)
        self.assert_refused(result, f"{target}/kit already exists; use --upgrade")
        self.assertEqual(sorted(p.name for p in (target / "kit").iterdir()), ["mine.txt"])
        self.assertFalse((target / "program").exists())
        self.assertEqual(self.leftovers(target), [])

    def test_upgrade_refuses_modified_kit(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        changed = target / "kit" / "scripts" / "common.py"
        changed.write_text(changed.read_text() + "# local edit\n")
        (target / "kit" / "extra.txt").write_text("x\n")
        result = self.install(target, "--upgrade")
        self.assert_refused(result, "modified: scripts/common.py")
        self.assertIn("added: extra.txt", result.stderr)
        self.assertTrue(changed.read_text().endswith("# local edit\n"))
        self.assertEqual(self.leftovers(target), [])

    def test_upgrade_replaces_kit_and_keeps_program(self):
        target = self.repo()
        self.assert_ok(self.install(target, "--preset", "python"))
        # Make the installed kit an older, unmodified one: an extra file recorded in its own manifest.
        (target / "kit" / "retired.txt").write_text("old\n")
        self.assertEqual(self.pk(target, "manifest", "write").returncode, 0)
        secret = target / "program" / "my-private-notes.md"
        secret.write_bytes(b"\x00\x01 project owned \xff\n")
        before = secret.read_bytes()
        result = self.install(target, "--upgrade")
        self.assert_ok(result)
        self.assertFalse((target / "kit" / "retired.txt").exists())
        self.assertEqual((target / "kit" / "MANIFEST.sha256").read_bytes(), (KIT / "MANIFEST.sha256").read_bytes())
        self.assertEqual(secret.read_bytes(), before)
        self.assertNotIn("my-private-notes", result.stdout + result.stderr)
        self.assertIn("program/ exists; left untouched", result.stdout)
        self.assertEqual(result.stdout.splitlines()[-1], f"installed program-kit {VERSION} into {target}")
        self.assertEqual(self.leftovers(target), [])
        self.assertEqual(self.pk(target, "manifest", "check").stdout, "manifest: clean\n")

    def test_upgrade_requires_existing_kit(self):
        target = self.repo()
        self.assert_refused(self.install(target, "--upgrade"), f"{target}/kit does not exist")
        self.assertFalse((target / "program").exists())

    def test_upgrade_uses_source_check_when_installed_kit_predates_manifest_command(self):
        target = self.repo()
        shutil.copytree(KIT, target / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        (target / "kit" / "scripts" / "manifest.py").unlink()
        pk_py = target / "kit" / "scripts" / "pk.py"
        pk_py.write_text(pk_py.read_text().replace(', "manifest")', ")"))
        import manifest
        manifest.write(target / "kit")
        self.assertEqual(self.pk(target, "manifest", "check").returncode, 1)  # the command really is gone
        self.assert_ok(self.install(target, "--upgrade"))
        self.assertEqual(self.pk(target, "manifest", "check").stdout, "manifest: clean\n")

    def test_upgrade_refuses_when_installed_kit_has_no_manifest(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        (target / "kit" / "MANIFEST.sha256").unlink()
        self.assert_refused(self.install(target, "--upgrade"), "MANIFEST.sha256")
        self.assertTrue((target / "kit" / "scripts" / "pk.py").is_file())

    def test_upgrade_does_not_trust_the_installed_kit_to_check_itself(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        (target / "kit" / "scripts" / "pk.py").write_text('print("manifest: clean")\n')
        (target / "kit" / "templates" / "BRIEF.md").write_text("edited\n")
        before = snapshot(target / "kit")
        result = self.install(target, "--upgrade")
        self.assert_refused(result, "modified: templates/BRIEF.md")
        self.assertEqual(snapshot(target / "kit"), before)
        self.assertEqual(self.leftovers(target), [])

    def test_upgrade_refuses_a_symlink_inside_the_installed_kit(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        os.symlink("VERSION", target / "kit" / "link")
        before = snapshot(target / "kit")
        self.assert_refused(self.install(target, "--upgrade"), "symlink")
        self.assertEqual(snapshot(target / "kit"), before)
        self.assertEqual(self.leftovers(target), [])

    def test_upgrade_never_reuses_an_existing_old_directory(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        stale = target / ".kit.old.12345"
        (stale / "sub").mkdir(parents=True)
        (stale / "sub" / "keep.txt").write_bytes(b"not ours\n")
        before_stale, before_kit = snapshot(stale), snapshot(target / "kit")
        result = self.install(target, "--upgrade", env=_env(PK_INSTALL_PID="12345"))
        self.assert_refused(result, ".kit.old.12345")
        self.assertEqual(snapshot(stale), before_stale)
        self.assertEqual(snapshot(target / "kit"), before_kit)
        self.assertEqual(self.leftovers(target), [".kit.old.12345"])

    def test_upgrade_does_not_follow_a_symlink_at_the_old_directory(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        victim = self.tmp / "victim"
        victim.mkdir()
        (victim / "precious.txt").write_bytes(b"precious\n")
        os.symlink(victim, target / ".kit.old.12345")
        before_victim, before_kit = snapshot(victim), snapshot(target / "kit")
        result = self.install(target, "--upgrade", env=_env(PK_INSTALL_PID="12345"))
        self.assert_refused(result, ".kit.old.12345")
        self.assertEqual(snapshot(victim), before_victim)
        self.assertEqual(snapshot(target / "kit"), before_kit)
        self.assertTrue((target / ".kit.old.12345").is_symlink())

    def test_fresh_install_never_reuses_an_existing_temporary_directory(self):
        target = self.repo()
        stale = target / ".kit.tmp.12345"
        stale.mkdir()
        (stale / "keep.txt").write_bytes(b"not ours\n")
        before = snapshot(stale)
        result = self.install(target, env=_env(PK_INSTALL_PID="12345"))
        self.assert_refused(result, ".kit.tmp.12345")
        self.assertEqual(snapshot(stale), before)
        self.assertFalse((target / "kit").exists())

    def test_a_non_numeric_pid_override_is_ignored(self):
        target = self.repo()
        self.assert_ok(self.install(target, env=_env(PK_INSTALL_PID="../x")))
        self.assertFalse((self.tmp / "x").exists())

    def test_cdpath_does_not_disturb_the_target_path(self):
        target = self.repo()
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        result = self.install(os.path.relpath(target, elsewhere), cwd=elsewhere,
                              env=_env(CDPATH=f".{os.pathsep}{self.tmp}{os.pathsep}/"))
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines()[-1], f"installed program-kit {VERSION} into {target}")

    def test_unknown_preset_copies_nothing(self):
        target = self.repo()
        self.assert_refused(self.install(target, "--preset", "cobol"), "preset")
        self.assertEqual(sorted(p.name for p in target.iterdir() if p.name != ".git"), [])

    def test_preset_needs_a_value_and_other_options_are_errors(self):
        target = self.repo()
        self.assert_refused(self.install(target, "--preset"), "--preset")
        self.assert_refused(self.install(target, "--force"), "--force")
        self.assert_refused(self.install(), "target")
        other = self.repo("other")
        self.assert_refused(self.install(target, other), "one target")
        self.assertEqual(sorted(p.name for p in target.iterdir() if p.name != ".git"), [])

    def test_target_must_be_a_git_top_level(self):
        target = self.repo()
        (target / "sub").mkdir()
        self.assert_refused(self.install(target / "sub"), "top level")
        plain = self.tmp / "plain"
        plain.mkdir()
        self.assert_refused(self.install(plain), "git")
        self.assert_refused(self.install(self.tmp / "missing"), "not a directory")
        self.assertEqual(sorted(p.name for p in (target / "sub").iterdir()), [])
        self.assertEqual(sorted(p.name for p in plain.iterdir()), [])

    def test_refuses_to_install_into_the_kits_own_repository(self):
        source = self.tmp / "source"
        source.mkdir()
        shutil.copy(INSTALL, source / "install.sh")
        shutil.copytree(KIT, source / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        subprocess.run(["git", "init", "-q", str(source)], check=True, env=_env())
        self.assert_refused(self.install(source, script=source / "install.sh"), "own repository")
        self.assertFalse((source / "program").exists())
        self.assertEqual(self.leftovers(source), [])

    def test_target_path_with_a_space_works(self):
        target = self.repo("my project")
        result = self.install(target)
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines()[-1], f"installed program-kit {VERSION} into {target}")
        self.assertTrue((target / "program" / "checks.yaml").is_file())

    def test_runs_from_any_directory_through_a_relative_path(self):
        target = self.repo()
        elsewhere = self.tmp / "elsewhere" / "deep"
        elsewhere.mkdir(parents=True)
        relative_script = os.path.relpath(INSTALL, elsewhere)
        relative_target = os.path.relpath(target, elsewhere)
        result = self.install(relative_target, script=relative_script, cwd=elsewhere)
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines()[-1], f"installed program-kit {VERSION} into {target}")

    def test_install_does_not_copy_caches(self):
        source = self.tmp / "source"
        source.mkdir()
        shutil.copy(INSTALL, source / "install.sh")
        shutil.copytree(KIT, source / "kit")
        (source / "kit" / "scripts" / "__pycache__").mkdir(exist_ok=True)
        (source / "kit" / "scripts" / "__pycache__" / "planted.cpython-311.pyc").write_bytes(b"x")
        (source / "kit" / "scripts" / "stray.pyc").write_bytes(b"x")
        (source / "kit" / "templates" / ".DS_Store").write_bytes(b"x")
        target = self.repo()
        self.assert_ok(self.install(target, script=source / "install.sh"))
        names = [p.name for p in (target / "kit").rglob("*")]
        for banned in ("__pycache__", "planted.cpython-311.pyc", "stray.pyc", ".DS_Store"):
            self.assertNotIn(banned, names)
        self.assertTrue((target / "kit" / "scripts" / "pk.py").is_file())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root ignores directory permissions")
    def test_failed_copy_leaves_no_temporary_directory(self):
        target = self.repo()
        target.chmod(0o555)
        self.addCleanup(target.chmod, 0o755)
        result = self.install(target)
        target.chmod(0o755)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("error: ", result.stderr)
        self.assertEqual(self.leftovers(target), [])
        self.assertFalse((target / "kit").exists())

    def _broken_source(self):
        source = self.tmp / "source"
        source.mkdir()
        shutil.copy(INSTALL, source / "install.sh")
        shutil.copytree(KIT, source / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        unreadable = source / "kit" / "templates" / "rulings.md"
        unreadable.chmod(0)
        self.addCleanup(unreadable.chmod, 0o644)
        return source / "install.sh"

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every file")
    def test_failed_copy_of_an_unreadable_source_file_leaves_nothing_behind(self):
        target = self.repo()
        result = self.install(target, script=self._broken_source())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("error: ", result.stderr)
        self.assertEqual(self.leftovers(target), [])
        self.assertFalse((target / "kit").exists())
        self.assertFalse((target / "program").exists())

    @unittest.skipIf(hasattr(os, "geteuid") and os.geteuid() == 0, "root reads every file")
    def test_failed_copy_during_upgrade_leaves_the_old_kit_byte_identical(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        before = snapshot(target / "kit")
        result = self.install(target, "--upgrade", script=self._broken_source())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("error: ", result.stderr)
        self.assertEqual(snapshot(target / "kit"), before)
        self.assertEqual(self.leftovers(target), [])

    def test_requires_python_3_11(self):
        target = self.repo()
        fake = self.tmp / "bin"
        fake.mkdir()
        (fake / "python3").write_text("#!/bin/sh\nexit 1\n")
        (fake / "python3").chmod(0o755)
        result = self.install(target, env=_env(PATH=f"{fake}{os.pathsep}{os.environ['PATH']}"))
        self.assert_refused(result, "python3 3.11")
        self.assertEqual(sorted(p.name for p in target.iterdir() if p.name != ".git"), [])

    def test_doctor_ok_after_install(self):
        for preset in ("go", "node", "python"):
            with self.subTest(preset=preset):
                target = self.repo(f"d-{preset}")
                self.assert_ok(self.install(target, "--preset", preset))
                doctor = self.pk(target, "doctor")
                self.assertEqual((doctor.returncode, doctor.stdout, doctor.stderr), (0, "doctor: ok\n", ""))


@unittest.skipUnless(shutil.which("dash"), "dash is not installed")
class InstallUnderDashTests(InstallCase):
    shell = "dash"

    def test_install_and_upgrade_under_dash(self):
        target = self.repo("dash target")
        result = self.install(target, "--preset", "node")
        self.assert_ok(result)
        self.assertEqual(result.stdout.splitlines()[-1], f"installed program-kit {VERSION} into {target}")
        upgrade = self.install(target, "--upgrade")
        self.assert_ok(upgrade)
        self.assertIn("program/ exists; left untouched", upgrade.stdout)
        self.assert_refused(self.install(target), "already exists; use --upgrade")


class ProjectPreparationTests(InstallCase):
    """Finding 8: a first commit made by following the README leaves a clean tree and tracks no bytecode."""

    LINES = ["__pycache__/", "*.pyc", ".worktrees/"]

    def test_install_creates_gitignore_with_the_three_lines(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        self.assertEqual((target / ".gitignore").read_text().splitlines(), self.LINES)

    def test_install_appends_only_the_missing_lines_and_keeps_existing_bytes(self):
        target = self.repo()
        original = b"# mine\r\nnode_modules/\r\n*.pyc\r\n.env"  # CRLF and no final newline
        (target / ".gitignore").write_bytes(original)
        self.assert_ok(self.install(target))
        data = (target / ".gitignore").read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual(data[len(original):], b"\n__pycache__/\n.worktrees/\n")

    def test_install_leaves_a_complete_gitignore_byte_identical_even_when_program_exists(self):
        target = self.repo()
        text = b"dist/\n.worktrees/\n*.pyc\n__pycache__/\n"
        (target / ".gitignore").write_bytes(text)
        (target / "program").mkdir()
        self.assert_ok(self.install(target))
        self.assertEqual((target / ".gitignore").read_bytes(), text)

    def test_install_adds_the_lines_when_program_already_exists(self):
        target = self.repo()
        (target / "program").mkdir()
        self.assert_ok(self.install(target))
        self.assertEqual((target / ".gitignore").read_text().splitlines(), self.LINES)

    def upgraded(self, ignore):
        """An installed project whose .gitignore is `ignore` (None: absent), upgraded; returns the target."""
        target = self.repo()
        self.assert_ok(self.install(target, "--preset", "python"))
        if ignore is None:
            (target / ".gitignore").unlink()
        else:
            (target / ".gitignore").write_bytes(ignore)
        program = snapshot(target / "program")
        self.assert_ok(self.install(target, "--upgrade"))
        self.assertEqual(snapshot(target / "program"), program)  # program/ is still not touched
        self.assertEqual(self.leftovers(target), [])
        return target

    def test_upgrade_appends_only_the_missing_lines_and_keeps_existing_bytes(self):
        original = b"# mine\r\nnode_modules/\r\n*.pyc\r\n.env"  # CRLF and no final newline
        data = (self.upgraded(original) / ".gitignore").read_bytes()
        self.assertTrue(data.startswith(original))
        self.assertEqual(data[len(original):], b"\n__pycache__/\n.worktrees/\n")  # exactly as a fresh install

    def test_upgrade_creates_gitignore_with_the_three_lines_when_it_is_absent(self):
        self.assertEqual((self.upgraded(None) / ".gitignore").read_text().splitlines(), self.LINES)

    def test_upgrade_leaves_a_complete_gitignore_byte_identical(self):
        text = b"dist/\n.worktrees/\n*.pyc\n__pycache__/\n"
        self.assertEqual((self.upgraded(text) / ".gitignore").read_bytes(), text)

    def test_upgrade_of_a_project_from_before_the_ignore_lines_gets_a_clean_first_gate(self):
        target = self.upgraded(b"dist/\n")  # an install made before the installer wrote the lines
        self.assertEqual((target / ".gitignore").read_bytes(), b"dist/\n__pycache__/\n*.pyc\n.worktrees/\n")
        (target / ".worktrees" / "t1").mkdir(parents=True)
        (target / ".worktrees" / "t1" / "x").write_text("x")
        (target / "kit" / "scripts" / "__pycache__").mkdir()
        (target / "kit" / "scripts" / "__pycache__" / "pk.cpython-311.pyc").write_bytes(b"x")
        status = subprocess.run(["git", "-C", str(target), "status", "--porcelain", "--untracked-files=all"],
                                capture_output=True, text=True, env=_env()).stdout
        self.assertNotIn(".worktrees", status)
        self.assertNotIn("__pycache__", status)

    def test_a_refused_upgrade_leaves_gitignore_alone(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        (target / ".gitignore").write_bytes(b"dist/\n")
        (target / "kit" / "scripts" / "pk.py").write_text("changed\n")
        self.assert_refused(self.install(target, "--upgrade"), "does not match its manifest")
        self.assertEqual((target / ".gitignore").read_bytes(), b"dist/\n")

    def test_a_failed_install_leaves_gitignore_alone(self):
        target = self.repo()
        self.assert_refused(self.install(target, "--preset", "cobol"), "preset")
        self.assertFalse((target / ".gitignore").exists())

    def clean_env(self):
        return {k: v for k, v in _env().items() if k != "PYTHONDONTWRITEBYTECODE"}

    def run_in(self, target, *argv):
        return subprocess.run(list(argv), cwd=target, capture_output=True, text=True, env=self.clean_env())

    def first_commit_is_clean(self, target):
        pk = [sys.executable, "kit/scripts/pk.py"]
        self.assertEqual(self.run_in(target, *pk, "doctor").stdout, "doctor: ok\n")
        for command in (["interview", "next"], ["register", "validate"], ["manifest", "check"], ["handoff", "check"]):
            self.run_in(target, *pk, *command)  # whatever they answer, they must leave no file behind
        self.run_in(target, "git", "add", "kit", "program", ".gitignore")
        commit = self.run_in(target, "git", "-c", "user.name=T", "-c", "user.email=t@example.com",
                             "commit", "-q", "-m", "Add program-kit")
        self.assertEqual(commit.returncode, 0, commit.stderr)
        self.assertEqual(self.run_in(target, "git", "status", "--short").stdout, "")
        tracked = self.run_in(target, "git", "ls-files").stdout.splitlines()
        self.assertEqual([name for name in tracked if name.endswith(".pyc") or "__pycache__" in name], [])
        # the tree the next gate sees is still clean after the commands run again
        self.run_in(target, *pk, "doctor")
        self.assertEqual(self.run_in(target, "git", "status", "--short").stdout, "")

    def test_readme_start_in_an_existing_repository_leaves_a_clean_tree(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        for step in ("git branch -m main", "sh install.sh", "kit/scripts/pk.py doctor", ".gitignore",
                     "git add kit program .gitignore"):
            self.assertIn(step, readme)
        target = self.tmp / "existing"
        target.mkdir()
        self.run_in(target, "git", "init", "-q")  # git's default branch name here is not main
        self.run_in(target, "git", "symbolic-ref", "HEAD", "refs/heads/master")
        self.run_in(target, "git", "branch", "-m", "main")  # README: the primary branch must be main
        result = self.install(target, "--preset", "python", env=self.clean_env())
        self.assert_ok(result)
        self.first_commit_is_clean(target)

    def test_readme_start_in_a_new_repository_leaves_a_clean_tree(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn("kit/scripts/pk.py init --preset python", readme)
        target = self.tmp / "fresh"
        target.mkdir()
        shutil.copytree(KIT, target / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        shutil.copy(ROOT / ".gitignore", target / ".gitignore")
        self.run_in(target, "git", "init", "-q")
        self.run_in(target, "git", "symbolic-ref", "HEAD", "refs/heads/main")
        self.assertEqual(self.run_in(target, sys.executable, "kit/scripts/pk.py", "init", "--preset", "python").returncode, 0)
        self.first_commit_is_clean(target)

    def test_readme_start_in_a_new_repository_works_from_a_bare_template_copy_without_gitignore(self):
        target = self.tmp / "bare"
        target.mkdir()
        shutil.copytree(KIT, target / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests"))
        self.run_in(target, "git", "init", "-q")
        self.run_in(target, "git", "symbolic-ref", "HEAD", "refs/heads/main")
        self.assertEqual(self.run_in(target, sys.executable, "kit/scripts/pk.py", "init", "--preset", "go").returncode, 0)
        self.first_commit_is_clean(target)


class KitTestsStayInTheKitRepositoryTests(InstallCase):
    """Finding 15: the kit's tests are not installed; an upgrade removes them from an older install."""

    def test_install_does_not_copy_kit_tests(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        self.assertFalse((target / "kit" / "tests").exists())
        self.assertNotIn("tests/", (target / "kit" / "MANIFEST.sha256").read_text())
        check = self.pk(target, "manifest", "check")
        self.assertEqual((check.returncode, check.stdout, check.stderr), (0, "manifest: clean\n", ""))

    def test_the_source_checkout_has_tests_but_the_install_does_not_need_them(self):
        self.assertTrue((KIT / "tests").is_dir())
        target = self.repo()
        self.assert_ok(self.install(target))
        self.assertEqual(sorted(p.name for p in (target / "kit").iterdir()),
                         sorted(p.name for p in KIT.iterdir() if p.name not in ("tests", "__pycache__", ".DS_Store")))

    def test_upgrade_from_an_install_that_has_kit_tests_works_and_removes_them(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        shutil.copytree(KIT / "tests", target / "kit" / "tests", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        lines = []  # an older kit recorded its tests in the manifest
        for path in sorted((target / "kit" / "tests").rglob("*")):
            if path.is_file():
                lines.append(f"{common.sha256_file(path)}  {path.relative_to(target / 'kit').as_posix()}\n")
        manifest_path = target / "kit" / "MANIFEST.sha256"
        manifest_path.write_text(manifest_path.read_text() + "".join(sorted(lines, key=lambda l: l.split("  ")[1])))
        result = self.install(target, "--upgrade")
        self.assert_ok(result)
        self.assertFalse((target / "kit" / "tests").exists())
        self.assertEqual(self.pk(target, "manifest", "check").stdout, "manifest: clean\n")
        self.assertEqual(self.leftovers(target), [])


    def test_a_file_added_under_kit_tests_of_an_installed_project_is_reported_and_refuses_the_upgrade(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        mine = target / "kit" / "tests" / "test_mine.py"
        mine.parent.mkdir()
        mine.write_text("# the project's own\n", encoding="utf-8")
        check = self.pk(target, "manifest", "check")
        self.assertEqual((check.returncode, check.stdout, check.stderr), (1, "", "error: added: tests/test_mine.py\n"))
        before = snapshot(target)
        result = self.install(target, "--upgrade")
        self.assert_refused(result, "does not match its manifest; refusing to upgrade")
        self.assertIn("error: added: tests/test_mine.py\n", result.stderr)
        self.assertEqual(snapshot(target), before)
        (target / "install.sh").write_text("#!/bin/sh\necho the project's own installer\n", encoding="utf-8")
        self.assert_refused(self.install(target, "--upgrade"), "refusing to upgrade")  # not the kit's installer
        self.assertTrue(mine.is_file())

    def test_upgrade_of_a_copy_of_the_whole_template_works_and_removes_its_tests(self):
        target = self.repo()
        shutil.copy(INSTALL, target / "install.sh")  # README, a new repository: the template's files, tests included
        shutil.copytree(KIT, target / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        self.assertTrue((target / "kit" / "tests" / "test_manifest.py").is_file())
        check = self.pk(target, "manifest", "check")
        self.assertEqual((check.returncode, check.stdout, check.stderr), (0, "manifest: clean\n", ""))
        self.assert_ok(self.install(target, "--upgrade"))
        self.assertFalse((target / "kit" / "tests").exists())
        self.assertEqual(self.leftovers(target), [])


class SourceManifestTests(InstallCase):
    """Lesser 15: a source kit that does not match its own manifest is refused with one error line."""

    def source(self):
        source = self.tmp / "source"
        source.mkdir()
        shutil.copy(INSTALL, source / "install.sh")
        shutil.copytree(KIT, source / "kit", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        return source

    def assert_one_line(self, result, fragment):
        self.assert_refused(result, fragment)
        self.assertEqual(len(result.stderr.splitlines()), 1, result.stderr)
        self.assertIn("manifest", result.stderr)

    def test_a_modified_source_file_is_refused_on_install(self):
        source = self.source()
        (source / "kit" / "templates" / "BRIEF.md").write_text("edited\n")
        target = self.repo()
        self.assert_one_line(self.install(target, script=source / "install.sh"), "modified: templates/BRIEF.md")
        self.assertEqual(sorted(p.name for p in target.iterdir() if p.name != ".git"), [])

    def test_an_added_and_a_missing_source_file_are_both_named(self):
        source = self.source()
        (source / "kit" / "extra.txt").write_text("x\n")
        (source / "kit" / "VERSION.bak").write_text("x\n")
        (source / "kit" / "rules" / "THROUGHPUT.md").unlink()
        target = self.repo()
        result = self.install(target, script=source / "install.sh")
        self.assert_one_line(result, "added: extra.txt")
        self.assertIn("missing: rules/THROUGHPUT.md", result.stderr)

    def test_a_stale_manifest_is_refused_on_upgrade_and_the_installed_kit_is_untouched(self):
        target = self.repo()
        self.assert_ok(self.install(target))
        source = self.source()
        (source / "kit" / "scripts" / "common.py").write_text(
            (source / "kit" / "scripts" / "common.py").read_text() + "# newer, manifest not rewritten\n")
        before = snapshot(target / "kit")
        self.assert_one_line(self.install(target, "--upgrade", script=source / "install.sh"),
                             "modified: scripts/common.py")
        self.assertEqual(snapshot(target / "kit"), before)
        self.assertEqual(self.leftovers(target), [])

    def test_a_missing_source_manifest_is_one_error_line(self):
        source = self.source()
        (source / "kit" / "MANIFEST.sha256").unlink()
        target = self.repo()
        result = self.install(target, script=source / "install.sh")
        self.assert_one_line(result, "MANIFEST.sha256")
        self.assertFalse((target / "kit").exists())

    def test_the_shipped_source_passes(self):
        self.assert_ok(self.install(self.repo()))


if __name__ == "__main__":
    unittest.main()
