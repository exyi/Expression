# Attribution to original authors of this code
# --------------------------------------------
# This code has been originally ported from the Fable project (https://fable.io)
# Copyright (c) Alfonso García-Caro and contributors.
#
# The original code was authored by
# - Alfonso Garcia-Caro (https://github.com/alfonsogarciacaro)
# - ncave (https://github.com/ncave)
#
# You can find the original implementation here:
# - https://github.com/fable-compiler/Fable/blob/nagareyama/src/fable-library/MailboxProcessor.ts


import asyncio
from queue import SimpleQueue
from threading import RLock
from typing import Any, Awaitable, Callable, Generic, Optional, TypeVar

from expression.system import CancellationToken, OperationCanceledError

from .aio import Continuation, from_continuations, start_immediate

Msg = TypeVar("Msg")
Reply = TypeVar("Reply")


class AsyncReplyChannel(Generic[Reply]):
    def __init__(self, fn: Callable[[Reply], None]) -> None:
        self.fn = fn

    def reply(self, r: Reply) -> None:
        self.fn(r)


class MailboxProcessor(Generic[Msg]):
    def __init__(self, cancellationToken: Optional[CancellationToken]) -> None:
        self.messages: SimpleQueue[Msg] = SimpleQueue()
        self.continuation: Optional[
            Continuation[Msg]
        ] = None  # Holds the continuation i.e the `done` callback of Async.from_continuations returned by `receive`.
        self.token = cancellationToken or CancellationToken.none()
        self.lock = RLock()
        self.loop = asyncio.get_event_loop()

    def post(self, msg: Msg) -> None:
        """Post a message synchronously to the mailbox processor.

        This method is not asynchronous since it's very fast to execute.
        It simply adds the message to the message queue of the mailbox
        processor and returns.

        Args:
            msg: Message to post.

        Returns: None
        """
        self.messages.put(msg)
        self.loop.call_soon_threadsafe(self.__process_events)

    def post_and_async_reply(self, build_message: Callable[[AsyncReplyChannel[Reply]], Msg]) -> Awaitable[Reply]:
        """Post a message asynchronously to the mailbox processor and
        wait for the reply.

        Args:
            build_message: A function that takes a reply channel
            (`AsyncReplyChannel[Reply]`) and returns a message to send
            to the mailbox processor. The message should contain the
            reply channel as e.g a tuple.

        Returns: The reply from mailbox processor.
        """

        result: Optional[Reply] = None
        continuation: Optional[
            Continuation[Reply]
        ] = None  # This is the continuation for the `done` callback of the awaiting poster.

        def check_completion() -> None:
            if result is not None and continuation is not None:
                continuation(result)

        def reply_callback(res: Reply):
            nonlocal result
            result = res
            check_completion()

        reply_channel = AsyncReplyChannel(reply_callback)
        self.messages.put(build_message(reply_channel))
        self.__process_events()

        def callback(
            done: Continuation[Reply], error: Continuation[Exception], cancel: Continuation[OperationCanceledError]
        ):
            nonlocal continuation
            continuation = done
            check_completion()

        return from_continuations(callback)

    def receive(self) -> Awaitable[Msg]:
        """Return an asynchronous computation which will consume the
        first message in arrival order. No thread is blocked while
        waiting for further messages. Raise a TimeoutException if the
        timeout is exceeded."""

        def callback(
            done: Continuation[Msg], error: Continuation[Exception], cancel: Continuation[OperationCanceledError]
        ):
            if self.continuation:
                raise Exception("Receive can only be called once!")

            self.continuation = done
            self.__process_events()

        return from_continuations(callback)

    def __process_events(self):
        if self.continuation is None:
            return

        with self.lock:
            if self.messages.empty():
                return
            msg = self.messages.get()
            self.continuation, cont = None, self.continuation

            if cont is not None:
                cont(msg)

    @staticmethod
    def start(
        body: Callable[["MailboxProcessor[Any]"], Awaitable[None]],
        cancellation_token: Optional[CancellationToken] = None,
    ) -> "MailboxProcessor[Any]":
        mbox: MailboxProcessor[Any] = MailboxProcessor(cancellation_token)
        start_immediate(body(mbox), cancellation_token)
        return mbox
