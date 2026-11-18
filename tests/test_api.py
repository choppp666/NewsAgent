"""接口自动化测试：用户管理与订阅偏好。

运行：pytest -v
"""

from __future__ import annotations

from fastapi.testclient import TestClient

API = "/api/v1"


# ========================================================== 用户接口


def test_health(client: TestClient) -> None:
    response = client.get("/api/health")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"]["status"] == "ok"


def test_create_user_success(client: TestClient) -> None:
    response = client.post(
        f"{API}/users",
        json={"nickname": "  小林  ", "identity": "后端开发实习生", "email": "xiaolin@example.com"},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["code"] == 0
    data = body["data"]
    assert data["id"] >= 1
    assert data["nickname"] == "小林"  # 首尾空格被清洗
    assert data["email"] == "xiaolin@example.com"
    assert data["created_at"] and data["updated_at"]


def test_create_user_without_email(client: TestClient) -> None:
    response = client.post(f"{API}/users", json={"nickname": "小王", "identity": "算法工程师"})
    assert response.status_code == 201
    assert response.json()["data"]["email"] is None


def test_create_user_empty_email_treated_as_null(client: TestClient) -> None:
    response = client.post(
        f"{API}/users", json={"nickname": "空邮箱", "identity": "学生", "email": "   "}
    )
    assert response.status_code == 201
    assert response.json()["data"]["email"] is None


def test_create_user_validation_errors(client: TestClient) -> None:
    response = client.post(f"{API}/users", json={"nickname": "", "identity": ""})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 422
    assert body["data"] is None or "errors" in body["data"]


def test_create_user_invalid_email(client: TestClient) -> None:
    response = client.post(
        f"{API}/users", json={"nickname": "张三", "identity": "学生", "email": "not-an-email"}
    )
    assert response.status_code == 422
    assert "邮箱" in response.json()["message"]


def test_get_user_not_found(client: TestClient) -> None:
    response = client.get(f"{API}/users/9999")
    assert response.status_code == 404
    assert response.json()["code"] == 404


def test_get_user(client: TestClient, user_id: int) -> None:
    response = client.get(f"{API}/users/{user_id}")
    assert response.status_code == 200
    assert response.json()["data"]["nickname"] == "小林"


def test_update_user_partial(client: TestClient, user_id: int) -> None:
    response = client.put(f"{API}/users/{user_id}", json={"nickname": "林同学"})
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["nickname"] == "林同学"
    # 未传的字段保持不变
    assert data["identity"] == "后端开发实习生"
    assert data["email"] == "xiaolin@example.com"


def test_update_user_clear_email(client: TestClient, user_id: int) -> None:
    response = client.put(f"{API}/users/{user_id}", json={"email": None})
    assert response.status_code == 200
    assert response.json()["data"]["email"] is None


def test_update_user_empty_payload(client: TestClient, user_id: int) -> None:
    response = client.put(f"{API}/users/{user_id}", json={})
    assert response.status_code == 400
    assert "没有任何需要更新的字段" in response.json()["message"]


def test_list_users(client: TestClient, user_id: int) -> None:
    client.post(f"{API}/users", json={"nickname": "小王", "identity": "算法工程师"})
    response = client.get(f"{API}/users?page=1&page_size=10")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 2


# ========================================================== 订阅偏好接口


def test_get_preferences_before_set(client: TestClient, user_id: int) -> None:
    response = client.get(f"{API}/users/{user_id}/preferences")
    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert body["data"] is None


def test_upsert_preferences_creates(client: TestClient, user_id: int) -> None:
    payload = {
        "topics": ["大模型 / LLM", "AI Agent"],
        "keywords": ["RAG", "微调"],
        "exclude_keywords": ["招聘"],
    }
    response = client.put(f"{API}/users/{user_id}/preferences", json=payload)
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["user_id"] == user_id
    assert data["topics"] == ["大模型 / LLM", "AI Agent"]
    assert data["keywords"] == ["RAG", "微调"]
    assert data["exclude_keywords"] == ["招聘"]

    # 再查一次，确认已落库
    again = client.get(f"{API}/users/{user_id}/preferences").json()["data"]
    assert again["topics"] == payload["topics"]


def test_upsert_preferences_overwrites(client: TestClient, user_id: int) -> None:
    client.put(
        f"{API}/users/{user_id}/preferences",
        json={"topics": ["A", "B", "C"], "keywords": ["k1"], "exclude_keywords": ["x"]},
    )
    response = client.put(
        f"{API}/users/{user_id}/preferences",
        json={"topics": ["D"], "keywords": [], "exclude_keywords": []},
    )
    data = response.json()["data"]
    assert data["topics"] == ["D"]      # 旧话题被整体替换
    assert data["keywords"] == []
    assert data["exclude_keywords"] == []


def test_upsert_preferences_normalizes_input(client: TestClient, user_id: int) -> None:
    """去空格、去重（忽略大小写）、字符串按逗号拆分。"""
    response = client.put(
        f"{API}/users/{user_id}/preferences",
        json={
            "topics": "  大模型 ,ai agent,AI Agent ,  多模态  ",
            "keywords": ["  RAG ", "rag"],
            "exclude_keywords": ["", "   "],
        },
    )
    data = response.json()["data"]
    assert data["topics"] == ["大模型", "ai agent", "多模态"]
    assert data["keywords"] == ["RAG"]
    assert data["exclude_keywords"] == []


def test_preferences_limit_exceeded(client: TestClient, user_id: int) -> None:
    response = client.put(
        f"{API}/users/{user_id}/preferences",
        json={"topics": [f"话题{i}" for i in range(21)]},
    )
    assert response.status_code == 422
    assert "关注话题" in response.json()["message"]


def test_preferences_user_not_found(client: TestClient) -> None:
    response = client.put(f"{API}/users/9999/preferences", json={"topics": ["A"]})
    assert response.status_code == 404

    response = client.get(f"{API}/users/9999/preferences")
    assert response.status_code == 404


def test_preferences_update_timestamp(client: TestClient, user_id: int) -> None:
    client.put(f"{API}/users/{user_id}/preferences", json={"topics": ["A"]})
    first = client.get(f"{API}/users/{user_id}/preferences").json()["data"]["updated_at"]

    client.put(f"{API}/users/{user_id}/preferences", json={"topics": ["B"]})
    second = client.get(f"{API}/users/{user_id}/preferences").json()["data"]["updated_at"]

    assert second >= first  # 词条变更会刷新订阅的更新时间
