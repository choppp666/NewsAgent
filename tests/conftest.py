"""pytest 公共夹具。

关键点：测试使用临时目录下的独立 SQLite 文件，
绝不会污染开发库 data/news_agent.db。
"""

from __future__ import annotations

import sys
from collections.abc import Generator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

# 保证从项目根目录运行 `pytest` 时能 import app 包
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import models  # noqa: E402,F401  # 注册模型
from app.core.database import Base, get_db  # noqa: E402
from app.main import app  # noqa: E402


@pytest.fixture()
def engine(tmp_path: Path) -> Generator[Engine, None, None]:
    """每个测试用例一个全新的临时数据库。"""
    db_path = tmp_path / "test.db"
    test_engine = create_engine(
        f"sqlite:///{db_path.as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=test_engine)
    yield test_engine
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


@pytest.fixture()
def client(engine: Engine) -> Generator[TestClient, None, None]:
    """覆盖 get_db 依赖，把请求打到临时数据库。

    注意：这里不用 with TestClient(app)，避免触发 lifespan 中的 init_db()
    在真实数据库上建表。
    """
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Generator[Session, None, None]:
        db = TestingSession()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    test_client = TestClient(app)
    try:
        yield test_client
    finally:
        app.dependency_overrides.clear()


@pytest.fixture()
def user_id(client: TestClient) -> int:
    """预置一个用户，返回其 ID。"""
    response = client.post(
        "/api/v1/users",
        json={"nickname": "小林", "identity": "后端开发实习生", "email": "xiaolin@example.com"},
    )
    assert response.status_code == 201
    return response.json()["data"]["id"]
