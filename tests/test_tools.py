import tempfile
from pathlib import Path

import pytest

from agent.tools import ProjectTools
from agent.errors import PathViolation, ToolError


@pytest.fixture
def project(tmp_path: Path) -> ProjectTools:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("print('hello')\nprint('world')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / ".env").write_text("SECRET=1\n", encoding="utf-8")
    return ProjectTools(tmp_path)


def test_list_files(project):
    files = project.list_files(".")
    assert "src/a.py" in files
    assert "README.md" in files
    assert not any(".env" in f for f in files)


def test_read_file_range(project):
    text = project.read_file("src/a.py", start=2, end=2)
    assert "print('world')" in text
    assert "hello" not in text


def test_write_backup_and_atomic(project, tmp_path):
    project.write_file("src/a.py", "new\n")
    assert (tmp_path / "src" / "a.py").read_text(encoding="utf-8") == "new\n"
    backups = list((tmp_path / ".deepseek_agent_backups").rglob("a.py"))
    assert backups, "backup should exist"


def test_path_escape_blocked(project):
    with pytest.raises(PathViolation):
        project.read_file("../outside.txt")


def test_sensitive_blocked(project):
    with pytest.raises(PathViolation):
        project.read_file(".env")


def test_create_existing_fails(project):
    with pytest.raises(ToolError):
        project.create_file("README.md", "x")


def test_patch_file(project):
    project.patch_file("src/a.py", [{"old": "hello", "new": "hi"}])
    content = (project.root / "src" / "a.py").read_text(encoding="utf-8")
    assert "hi" in content and "hello" not in content


def test_patch_requires_unique(project):
    (project.root / "dup.txt").write_text("x\nx\n", encoding="utf-8")
    with pytest.raises(ToolError):
        project.patch_file("dup.txt", [{"old": "x", "new": "y"}])


def test_search(project):
    hits = project.search_files("hello")
    assert any(h["path"] == "src/a.py" for h in hits)


def test_move_file(project):
    project.move_file("README.md", "docs/README.md")
    assert (project.root / "docs" / "README.md").is_file()
    assert not (project.root / "README.md").exists()
