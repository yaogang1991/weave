"""Tests for ConftestDbChecker — validates conftest.py database initialization (#1001)."""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from core.models import CriterionType, SuccessCriterion
from evaluator.checkers.conftest_db import ConftestDbChecker
from evaluator.models import CheckSeverity, EvaluationContext


@pytest.fixture
def checker() -> ConftestDbChecker:
    return ConftestDbChecker()


def _make_context(work_dir: Path) -> EvaluationContext:
    return EvaluationContext(work_dir=work_dir)


def _write(work_dir: Path, path: str, content: str) -> Path:
    fpath = work_dir / path
    fpath.parent.mkdir(parents=True, exist_ok=True)
    fpath.write_text(textwrap.dedent(content), encoding="utf-8")
    return fpath


def _criterion() -> SuccessCriterion:
    return SuccessCriterion(
        type=CriterionType.CONFTEST_DB_INIT,
        description="conftest.py DB init",
    )


class TestConftestDbCheckerPass:
    """Cases where conftest.py is correctly configured."""

    def test_correct_conftest_with_models(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"

            class Item(Base):
                __tablename__ = "items"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from models import Base, User, Item
            from sqlalchemy import create_engine

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(bind=engine)
                yield engine
                Base.metadata.drop_all(bind=engine)
                engine.dispose()
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert result.passed, result.message

    def test_star_import_passes(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from models import *
            from sqlalchemy import create_engine

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(bind=engine)
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert result.passed, result.message

    def test_no_conftest_skips(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert result.passed
        assert result.severity == CheckSeverity.WARNING

    def test_no_models_module_passes(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from myapp.db import Base
            Base.metadata.create_all(bind=engine)
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert result.passed, result.message


class TestConftestDbCheckerFail:
    """Cases where conftest.py has database initialization issues."""

    def test_no_create_all(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from models import Base, User
            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert not result.passed
        assert "create_all()" in result.message

    def test_bare_base_import(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase
            from sqlalchemy import create_engine

            class Base(DeclarativeBase):
                pass

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(bind=engine)
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert not result.passed
        assert "sqlalchemy.orm" in result.message or "empty" in result.message

    def test_no_model_imports(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(bind=engine)
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert not result.passed

    def test_imports_from_models_but_no_actual_classes(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"

            class Item(Base):
                __tablename__ = "items"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from models import Base
            from sqlalchemy import create_engine

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(bind=engine)
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert not result.passed
        assert "model classes" in result.message

    def test_multiple_issues(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase
            from sqlalchemy import create_engine

            class Base(DeclarativeBase):
                pass

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert not result.passed
        assert "create_all()" in result.message
        assert "sqlalchemy.orm" in result.message


class TestConftestDbCheckerEdgeCases:
    """Edge cases and boundary conditions."""

    def test_app_models_path(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "app/models.py", textwrap.dedent("""\
            from sqlalchemy.orm import DeclarativeBase

            class Base(DeclarativeBase):
                pass

            class User(Base):
                __tablename__ = "users"
        """))
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from app.models import User
            from app.models import Base
            Base.metadata.create_all(bind=engine)
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert result.passed, result.message

    def test_declarative_base_antipattern(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "tests/conftest.py", textwrap.dedent("""\
            from sqlalchemy.ext.declarative import declarative_base
            from sqlalchemy import create_engine

            Base = declarative_base()

            @pytest.fixture
            def db_engine():
                engine = create_engine("sqlite:///:memory:")
                Base.metadata.create_all(bind=engine)
                yield engine
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert not result.passed
        assert "sqlalchemy.orm" in result.message or "empty" in result.message

    def test_wrong_criterion_type(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        crit = SuccessCriterion(
            type=CriterionType.FILE_EXISTS,
            description="wrong type",
        )
        result = checker.check(crit, _make_context(tmp_path))
        assert not result.passed
        assert "Unhandled" in result.message

    def test_root_conftest(
        self, checker: ConftestDbChecker, tmp_path: Path,
    ) -> None:
        _write(tmp_path, "conftest.py", textwrap.dedent("""\
            from models import Base, User
            Base.metadata.create_all(bind=engine)
        """))
        result = checker.check(_criterion(), _make_context(tmp_path))
        assert result.passed, result.message
