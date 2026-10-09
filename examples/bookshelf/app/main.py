from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="bookshelf")

BOOKS = [{"id": i, "title": f"Book {i}", "price": 10.0 + i} for i in range(1, 26)]


def paginate(items: list[dict], page: int, size: int) -> list[dict]:
    start = page * size
    return items[start : start + size]


@app.get("/books")
def list_books(page: int = Query(1, ge=1), size: int = Query(10, ge=1, le=50)):
    return {"page": page, "size": size, "total": len(BOOKS), "items": paginate(BOOKS, page, size)}


@app.get("/books/{book_id}")
def get_book(book_id: int):
    for b in BOOKS:
        if b["id"] == book_id:
            return b
    raise HTTPException(404, "not found")
