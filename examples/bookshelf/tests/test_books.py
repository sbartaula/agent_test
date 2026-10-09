from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def test_get_book():
    assert client.get("/books/3").json()["title"] == "Book 3"


def test_missing_book():
    assert client.get("/books/999").status_code == 404


def test_first_page_has_first_books():
    body = client.get("/books?page=1&size=10").json()
    assert [b["id"] for b in body["items"]] == list(range(1, 11))


def test_last_page_partial():
    body = client.get("/books?page=3&size=10").json()
    assert [b["id"] for b in body["items"]] == [21, 22, 23, 24, 25]
