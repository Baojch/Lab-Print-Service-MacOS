#!/usr/bin/env python3

import http.client
import http.server
import json
import ssl
import sys
import urllib.parse


LISTEN_HOST = "127.0.0.1"
LISTEN_PORT = 18085
UPSTREAM_HOST = "mprint.pjlab.org.cn"
UPSTREAM_PORT = 8085
MAX_REQUEST_BYTES = 1024 * 1024 * 1024
AD_AUTO_SETTING_PATH = "/cpms/api/system/getByCode/sys_identify_AD_auto"
OPEN_AUTH_LIST_PATH = "/cpms/api/login/openAuthList"

HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "proxy-connection",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "expect",
}


class BridgeHandler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "PJLabPrintBridge/1.7"

    def log_message(self, format_string, *args):
        sys.stderr.write(
            "%s - - [%s] %s\n"
            % (self.client_address[0], self.log_date_time_string(), format_string % args)
        )
        sys.stderr.flush()

    def _forward_chunked_body(self, connection):
        total = 0
        while True:
            size_line = self.rfile.readline(65537)
            if not size_line or len(size_line) > 65536:
                raise ValueError("invalid chunk header")
            size_text = size_line.split(b";", 1)[0].strip()
            size = int(size_text, 16)
            if size == 0:
                while True:
                    trailer = self.rfile.readline(65537)
                    if trailer in (b"\r\n", b"\n", b""):
                        break
                connection.send(b"0\r\n\r\n")
                break
            total += size
            if total > MAX_REQUEST_BYTES:
                raise ValueError("request body is too large")
            connection.send(("%x\r\n" % size).encode("ascii"))
            remaining = size
            while remaining:
                chunk = self.rfile.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("request body ended early")
                connection.send(chunk)
                remaining -= len(chunk)
            connection.send(b"\r\n")
            if self.rfile.read(2) != b"\r\n":
                raise ValueError("invalid chunk ending")

    def _forward_fixed_body(self, connection, length):
        if length < 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("request body is too large")
        remaining = length
        while remaining:
            chunk = self.rfile.read(min(1024 * 1024, remaining))
            if not chunk:
                raise ValueError("request body ended early")
            connection.send(chunk)
            remaining -= len(chunk)

    def _send_health(self):
        body = json.dumps(
            {
                "status": "ok",
                "upstream": f"https://{UPSTREAM_HOST}:{UPSTREAM_PORT}",
                "manual_ad_login": True,
            },
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200, "OK")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_manual_ad_setting(self):
        body = json.dumps(
            {
                "code": 200,
                "msg": "操作成功",
                "data": "false",
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200, "OK")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _send_ad_auth_list(self):
        body = json.dumps(
            {
                "code": 200,
                "msg": "成功",
                "data": [
                    {
                        "code": "sys_identify_AD",
                        "value": True,
                        "name": "AD域认证",
                    }
                ],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.send_response(200, "OK")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def _origin_form_path(self):
        if self.path.startswith(("http://", "https://")):
            parsed = urllib.parse.urlsplit(self.path)
            normalized = parsed.path or "/"
            if parsed.query:
                normalized = f"{normalized}?{parsed.query}"
            return normalized
        return self.path

    def _handle(self):
        request_target = self._origin_form_path()
        if self.command == "GET" and request_target == "/__pjlab_bridge_health__":
            self._send_health()
            return
        if self.command == "GET" and request_target.split("?", 1)[0] == AD_AUTO_SETTING_PATH:
            self._send_manual_ad_setting()
            return
        if self.command == "GET" and request_target.split("?", 1)[0] == OPEN_AUTH_LIST_PATH:
            self._send_ad_auth_list()
            return

        headers_sent = False
        connection = None
        try:
            headers = {}
            for name, value in self.headers.items():
                if name.lower() not in HOP_BY_HOP_HEADERS and name.lower() != "host":
                    headers[name] = value
            headers["Host"] = f"{UPSTREAM_HOST}:{UPSTREAM_PORT}"
            headers["Connection"] = "close"

            transfer_encoding = self.headers.get("Transfer-Encoding", "").lower()
            is_chunked = "chunked" in transfer_encoding
            content_length_text = self.headers.get("Content-Length")
            content_length = int(content_length_text) if content_length_text else 0
            if is_chunked:
                headers.pop("Content-Length", None)
                headers["Transfer-Encoding"] = "chunked"

            connection = http.client.HTTPSConnection(
                UPSTREAM_HOST,
                UPSTREAM_PORT,
                timeout=180,
                context=ssl.create_default_context(),
            )
            connection.putrequest(
                self.command,
                request_target,
                skip_host=True,
                skip_accept_encoding=True,
            )
            for name, value in headers.items():
                connection.putheader(name, value)
            connection.endheaders()

            if is_chunked:
                self._forward_chunked_body(connection)
            elif content_length:
                self._forward_fixed_body(connection, content_length)

            response = connection.getresponse()

            self.send_response(response.status, response.reason)
            for name, value in response.getheaders():
                lowered = name.lower()
                if lowered in HOP_BY_HOP_HEADERS:
                    continue
                self.send_header(name, value)
            self.send_header("Connection", "close")
            self.end_headers()
            headers_sent = True
            if self.command != "HEAD":
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
            self.close_connection = True
        except Exception as exc:
            self.log_error("%s %s failed: %r", self.command, self.path, exc)
            if not headers_sent:
                error_body = json.dumps(
                    {"code": 502, "msg": "PJLab local HTTPS bridge error"},
                    ensure_ascii=False,
                ).encode("utf-8")
                self.send_response(502, "Bad Gateway")
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(error_body)))
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(error_body)
            self.close_connection = True
        finally:
            if connection is not None:
                connection.close()

    do_GET = _handle
    do_HEAD = _handle
    do_POST = _handle
    do_PUT = _handle
    do_PATCH = _handle
    do_DELETE = _handle
    do_OPTIONS = _handle


def main():
    server = http.server.ThreadingHTTPServer((LISTEN_HOST, LISTEN_PORT), BridgeHandler)
    print(
        f"PJLab HTTPS bridge: http://{LISTEN_HOST}:{LISTEN_PORT} "
        f"-> https://{UPSTREAM_HOST}:{UPSTREAM_PORT}",
        flush=True,
    )
    server.serve_forever()


if __name__ == "__main__":
    main()
