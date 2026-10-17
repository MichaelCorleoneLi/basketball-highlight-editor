import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Dict, Iterable, TextIO

from .protocol import ProtocolError, error_response, parse_request, response
from .service import EngineService

# 只读且耗时的命令放到后台线程执行，避免审核页的封面抽帧（每次一个 FFmpeg 进程）
# 堵住 stdin 主循环，让标记球员、审核等轻量写操作排在后面迟迟得不到响应。
# 响应按 request_id 关联，调用方不依赖返回顺序。
BACKGROUND_COMMANDS = frozenset({"extract_preview"})
BACKGROUND_WORKERS = 2


def _handle_line(service: EngineService, line: str) -> Dict[str, Any]:
    request_id = None
    try:
        request = parse_request(line)
        request_id = request.get("request_id")
        result = service.handle(request["command"], request.get("payload", {}))
        return response(request_id, result)
    except ProtocolError as exc:
        return error_response(request_id, exc.code, exc.message, exc.details)
    except Exception as exc:
        return error_response(request_id, "ENGINE_ERROR", str(exc))


def _command_of(line: str) -> str | None:
    try:
        command = json.loads(line).get("command")
    except (ValueError, AttributeError):
        return None
    return command if isinstance(command, str) else None


def serve(service: EngineService, lines: Iterable[str], output: TextIO) -> None:
    write_lock = threading.Lock()

    def write(payload: Dict[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with write_lock:
            output.write(encoded + "\n")
            output.flush()

    with ThreadPoolExecutor(
        max_workers=BACKGROUND_WORKERS,
        thread_name_prefix="engine-request",
    ) as executor:
        for raw_line in lines:
            line = raw_line.strip()
            if not line:
                continue
            if _command_of(line) in BACKGROUND_COMMANDS:
                executor.submit(lambda line=line: write(_handle_line(service, line)))
            else:
                write(_handle_line(service, line))


def main() -> int:
    service = EngineService()
    try:
        serve(service, sys.stdin, sys.stdout)
    finally:
        service.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
