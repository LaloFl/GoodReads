from functools import cached_property
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qsl, urlparse
import os
import re
import uuid
from bs4 import BeautifulSoup
import redis


# Código basado en:
# https://realpython.com/python-http-server/
# https://docs.python.org/3/library/http.server.html
# https://docs.python.org/3/library/http.cookies.html


mapping = [
    (r"^/$", "get_index"),
    (r"^/books/(?P<book_id>\d+)$", "get_book"),
    (r"^/books/search$", "get_search_books"),
]


class WebRequestHandler(BaseHTTPRequestHandler):
    @cached_property
    def cookies(self):
        return SimpleCookie(self.headers.get("Cookie"))

    def get_method(self, path):
        for pattern, method in mapping:
            match = re.match(pattern, path)
            if match:
                return (method, match.groupdict())

    def do_GET(self):
        self.url = urlparse(self.path)
        method = self.get_method(self.url.path)
        if method:
            method_name, dict_params = method
            method = getattr(self, method_name)
            method(**dict_params)
            return
        else:
            self.send_error(404, "Not Found")

    def get_index(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        with open("html/index.html", "r") as f:
            response = f.read()
        return self.wfile.write(response.encode("utf-8"))

    def get_book(self, book_id):
        session_id = self.get_book_session()
        r = redis.StrictRedis(
            host="localhost", port=6379, db=0, charset="utf-8", decode_responses=True
        )
        if r.exists(f"book{book_id}"):
            book_suggestion, read_again = self.get_book_suggestion(session_id, book_id)
            f = r.get(f"book{book_id}")
            if book_suggestion:
                f = f.replace(
                    "</html>", f"<h2>Libros sugeridos:</h2>{book_suggestion}</html>"
                )
            if read_again:
                f = f.replace("</html>", f"<h2>Vuelve a leer:</h2>{read_again}</html>")
            f = f.replace(
                "</html>",
                '<br><h2><a href="\\">Volver a menú</a></h2></html>',
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.set_book_cookie(session_id)
            self.end_headers()
            return self.wfile.write(f.encode("utf-8"))
        return self.send_error(404, "Not Found in Redis")

    def get_search_books(self):
        query_data = dict(parse_qsl(self.url.query))
        params = [
            query_data.get("author"),
            query_data.get("title"),
            query_data.get("description"),
        ]
        if not any(params):
            print("no params")
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            with open("html/search.html", "r") as f:
                response = f.read()
            return self.wfile.write(response.encode("utf-8"))

        r = redis.StrictRedis(
            host="localhost", port=6379, db=0, charset="utf-8", decode_responses=True
        )
        books = r.keys("book*")
        books_found = set()
        for book in books:
            author = query_data.get("author")
            title = query_data.get("title")
            description = query_data.get("description")

            if title:
                res_title, _, __ = self.get_book_info(book.split("book")[1])
                if title.lower() not in res_title.lower():
                    continue
            if author:
                _, res_author, __ = self.get_book_info(book.split("book")[1])
                if author.lower() not in res_author.lower():
                    continue
            if description:
                _, __, res_desc = self.get_book_info(book.split("book")[1])
                if description.lower() not in res_desc.lower():
                    continue
            books_found.add(book)

        r.connection_pool.disconnect()

        with open("html/search.html") as f:
            response = f.read()
        if books_found:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            for book in books_found:
                book_info = self.get_book_info(book.split("book")[1])
                response += f'<br><li><h3><a href="/books/{book.split("book")[1]}">{book_info[0]}</a></h3><p>{book_info[1]}</p><p>{book_info[2]}</p></li>'
            return self.wfile.write(response.encode("utf-8"))
        response += "<p>No books found</p>"
        self.send_response(404)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        return self.wfile.write(response.encode("utf-8"))

    def set_book_cookie(self, session_id, max_age=100):
        c = SimpleCookie()
        c["session"] = session_id
        c["session"]["max-age"] = max_age
        self.send_header("Set-Cookie", c.output(header=""))

    def get_book_session(self):
        c = self.cookies
        if not c:
            print("No cookie")
            c = SimpleCookie()
            c["session"] = uuid.uuid4()
        else:
            print("Cookie found")
        return c.get("session").value

    def get_book_suggestion(self, session_id, book_id):
        r = redis.StrictRedis(
            host="localhost", port=6379, db=0, charset="utf-8", decode_responses=True
        )
        books = r.keys("book*")
        books_read = r.lrange(session_id, 0, -1)
        if f"book{book_id}" not in books_read:
            r.rpush(session_id, f"book{book_id}")
        suggestions = ""
        r_a = ""
        for book in books:
            if book[-1] == book_id:
                continue
            if book not in books_read:
                sugg_title, _, __ = self.get_book_info(book[-1])
                if sugg_title:
                    suggestions += (
                        f'<li><a href="/books/{book[-1]}">{sugg_title}</a></li>'
                    )
                continue
            r_a_title, _, __ = self.get_book_info(book[-1])
            if r_a_title:
                r_a += f'<li><a href="/books/{book[-1]}">{r_a_title}</a></li>'

        r.connection_pool.disconnect()
        return suggestions, r_a

    def get_book_info(self, book_id):
        r = redis.StrictRedis(
            host="localhost", port=6379, db=0, charset="utf-8", decode_responses=True
        )
        soup = BeautifulSoup(r.get(f"book{book_id}"), "html.parser")

        title = soup.find("h2").string

        author, *desc = soup.find_all("p")
        desc_str = ""

        for i in range(len(desc)):
            desc_str += desc[i].string

        r.connection_pool.disconnect()
        return title, author.string, desc_str


def set_redis_keys():
    r = redis.StrictRedis(host="localhost", port=6379, db=0)
    for book in os.listdir("html/books"):
        with open(f"html/books/{book}") as book_file:
            if book.startswith("book"):
                r.set(f'{book.split(".")[0]}', book_file.read())
    r.connection_pool.disconnect()


if __name__ == "__main__":
    print("Server starting...")
    server = HTTPServer(("0.0.0.0", 8000), WebRequestHandler)
    set_redis_keys()
    server.serve_forever()
