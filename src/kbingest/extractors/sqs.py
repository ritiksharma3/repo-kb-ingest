"""AWS SQS: aws-sdk v2 (sqs.sendMessage), SDK v3 commands, sqs-consumer."""
from __future__ import annotations

from ..model import Facts
from .base import FileCtx, call_args, ln, prop_value, string_value, txt, walk

NAME = "sqs"
SEND = {"sendMessage", "sendMessageBatch"}
RECV = {"receiveMessage"}
CMD_SEND = {"SendMessageCommand", "SendMessageBatchCommand"}
CMD_RECV = {"ReceiveMessageCommand"}


def _queue_name(url: str | None) -> str | None:
    if not url:
        return None
    return url.rstrip("/").rsplit("/", 1)[-1] or None


def extract(ctxs: dict[str, FileCtx], facts: Facts) -> None:
    for path in sorted(ctxs):
        ctx = ctxs[path]
        if not ctx.has_import("aws-sdk", "@aws-sdk/client-sqs", "sqs-consumer"):
            continue
        for n in walk(ctx.root):
            direction = lib = cfg = None
            if n.type == "call_expression":
                fn = n.child_by_field_name("function")
                args = call_args(n)
                if fn.type == "member_expression":
                    prop = txt(fn.child_by_field_name("property"))
                    obj = txt(fn.child_by_field_name("object"))
                    if prop in SEND | RECV and args:
                        direction, lib, cfg = ("send" if prop in SEND else "receive"), "aws-sdk", args[0]
                    elif prop == "create" and obj in ("Consumer",) and args:
                        direction, lib, cfg = "receive", "sqs-consumer", args[0]
            elif n.type == "new_expression":
                c = n.child_by_field_name("constructor")
                args = call_args(n)
                nm = txt(c)
                if nm in CMD_SEND | CMD_RECV and args:
                    direction, lib, cfg = ("send" if nm in CMD_SEND else "receive"), "@aws-sdk/client-sqs", args[0]
            if direction is None:
                continue
            line = ln(n)
            qv = prop_value(cfg, "QueueUrl") or prop_value(cfg, "queueUrl")
            expr = txt(qv) if qv is not None else "<no QueueUrl>"
            url = string_value(qv)
            name = _queue_name(url)
            facts.add("queues", file_path=path, line=line, direction=direction, queue_name=name,
                      library=lib, status="verified" if name else "unverified", expression=expr,
                      enclosing_symbol=ctx.enclosing(line), line_hash=ctx.lh(line),
                      origin="source", extractor=NAME)
