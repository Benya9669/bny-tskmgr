from __future__ import annotations

import argparse
import logging
import socketserver
from pathlib import Path


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("smtp-sink")


class SmtpSinkHandler(socketserver.StreamRequestHandler):
    def write_response(self, response: str) -> None:
        self.wfile.write(f"{response}\r\n".encode("ascii"))
        self.wfile.flush()

    def handle(self) -> None:
        self.write_response("220 taskflow-smtp-sink ESMTP")
        message: list[bytes] = []
        in_data = False
        while line := self.rfile.readline():
            if in_data:
                if line == b".\r\n":
                    self.server.save_message(b"".join(message))  # type: ignore[attr-defined]
                    message.clear()
                    in_data = False
                    self.write_response("250 queued")
                else:
                    message.append(line[1:] if line.startswith(b"..") else line)
                continue

            command = line.decode("utf-8", errors="replace").strip().upper()
            if command.startswith(("EHLO", "HELO")):
                self.write_response("250 taskflow-smtp-sink")
            elif command.startswith(("MAIL FROM:", "RCPT TO:")):
                self.write_response("250 ok")
            elif command == "DATA":
                in_data = True
                self.write_response("354 end with <CRLF>.<CRLF>")
            elif command in {"RSET", "NOOP"}:
                self.write_response("250 ok")
            elif command == "QUIT":
                self.write_response("221 bye")
                return
            else:
                self.write_response("502 command not implemented")


class ThreadingSmtpSink(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, address: tuple[str, int], output: Path):
        super().__init__(address, SmtpSinkHandler)
        self.output = output

    def save_message(self, message: bytes) -> None:
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(message)
        logger.info("Saved SMTP message to %s", self.output)


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal SMTP sink for TaskFlow integration tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=1025)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    with ThreadingSmtpSink((args.host, args.port), args.output.resolve()) as server:
        logger.info("SMTP sink listening on %s:%s", args.host, args.port)
        server.serve_forever()


if __name__ == "__main__":
    main()
