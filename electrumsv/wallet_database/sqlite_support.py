import queue
import sqlite3
import threading
import traceback
from typing import Optional, List, Tuple, Callable, Any

from ..constants import DATABASE_EXT
from ..logs import logs


def max_sql_variables():
    """Get the maximum number of arguments allowed in a query by the current
    sqlite3 implementation.

    ESV amendment: Report that on CentOS the following error occurs:
       "sqlite3.OperationalError: too many terms in compound SELECT"
    This is another limit, likely lower: SQLITE_LIMIT_COMPOUND_SELECT

    Returns
    -------
    int
        inferred SQLITE_MAX_VARIABLE_NUMBER
    """
    db = sqlite3.connect(':memory:')
    cur = db.cursor()
    cur.execute('CREATE TABLE t (test)')
    low, high = 0, 100000
    while (high - 1) > low:
        guess = (high + low) // 2
        query = 'INSERT INTO t VALUES ' + ','.join(['(?)' for _ in
                                                    range(guess)])
        args = [str(i) for i in range(guess)]
        try:
            cur.execute(query, args)
        except sqlite3.OperationalError as e:
            es = str(e)
            if "too many SQL variables" in es or "too many terms in compound SELECT" in es:
                high = guess
            else:
                raise
        else:
            low = guess
    cur.close()
    db.close()
    return low

# https://stackoverflow.com/a/36788489
SQLITE_MAX_VARS = max_sql_variables()


class WriteDisabledError(Exception):
    pass


WriteCallbackType = Callable[[sqlite3.Connection], None]
CompletionCallbackType = Callable[[bool], None]
WriteEntryType = Tuple[WriteCallbackType, Optional[CompletionCallbackType]]

class SqliteWriteDispatcher:
    """
    This is a relatively simple write batcher for Sqlite that keeps all the writes on one thread,
    in order to avoid conflicts. Any higher level context that invokes a write, can choose to
    get notified on completion. If an exception happens in the course of a writer, the exception
    is passed back to the invoker in the completion notification.

    Completion notifications are done in a thread so as to not block the write dispatcher.

    TODO: Allow writes to be wrapped with async logic so that async coroutines can do writes
    in their natural fashion.
    """
    def __init__(self, db_context: "DatabaseContext") -> None:
        self._db_context = db_context
        self._logger = logs.get_logger(self.__class__.__name__)

        self._writer_queue = queue.Queue()
        self._writer_thread = threading.Thread(target=self._writer_thread_main, daemon=True)
        self._writer_loop_event = threading.Event()
        self._callback_queue = queue.Queue()
        self._callback_thread = threading.Thread(target=self._callback_thread_main, daemon=True)
        self._callback_loop_event = threading.Event()

        self._allow_puts = True
        self._is_alive = True
        self._exit_when_empty = False

        self._writer_thread.start()
        self._callback_thread.start()

    def _writer_thread_main(self) -> None:
        self._db = self._db_context.acquire_connection()

        maximum_batch_size = 10
        write_entries: List[WriteEntryType] = []
        write_entry_backlog: List[WriteEntryType] = []
        while self._is_alive:
            self._writer_loop_event.set()

            if len(write_entry_backlog):
                assert maximum_batch_size == 1
                write_entries = [ write_entry_backlog.pop(0) ]
            else:
                # Block until we have at least one write action. If we already have write
                # actions at this point, it is because we need to retry after a transaction
                # was rolled back.
                try:
                    write_entry: WriteEntryType = self._writer_queue.get(timeout=0.1)
                except queue.Empty:
                    if self._exit_when_empty:
                        return
                    continue
                write_entries = [ write_entry ]

            # Gather the rest of the batch for this transaction.
            while len(write_entries) < maximum_batch_size and not self._writer_queue.empty():
                write_entries.append(self._writer_queue.get_nowait())

            # Using the connection as a context manager, apply the batch as a transaction.
            completion_callbacks: List[Tuple[CompletionCallbackType, bool]] = []
            try:
                with self._db:
                    # We have to force a grouped statement transaction with the explicit 'begin'.
                    self._db.execute('begin')
                    for write_callback, completion_callback in write_entries:
                        write_callback(self._db)
                        if completion_callback is not None:
                            completion_callbacks.append((completion_callback, None))
                # The transaction was successfully committed.
            except Exception as e:
                # Exception: This is caught because we need to relay any exception to the
                # calling context's completion notification callback.
                self._logger.exception("Database write failure", exc_info=e)
                # The transaction was rolled back.
                if len(write_entries) > 1:
                    self._logger.debug("Retrying with batch size of 1")
                    # We're going to try and reapply the write actions one by one.
                    maximum_batch_size = 1
                    write_entry_backlog = write_entries
                    continue
                # We applied the batch actions one by one. If there was an error with this action
                # then we've logged it, so we can discard it for lack of any other option.
                if write_entries[0][1] is not None:
                    completion_callbacks.append((write_entries[0][1], e))
            else:
                if len(write_entries) > 1:
                    self._logger.debug("Invoked %d write callbacks", len(write_entries))

            for completion_callback in completion_callbacks:
                self._callback_queue.put_nowait(completion_callback)

    def _callback_thread_main(self) -> None:
        while self._is_alive:
            self._callback_loop_event.set()

            # A perpetually blocking get will not get interrupted by CTRL+C.
            try:
                callback, exc_value = self._callback_queue.get(timeout=0.2)
            except queue.Empty:
                if self._exit_when_empty:
                    return
                continue

            try:
                callback(exc_value)
            except Exception as e:
                traceback.print_exc()
                self._logger.exception("Exception within completion callback", exc_info=e)

    def put(self, write_entry: WriteEntryType) -> None:
        # If the writer is closed, then it is expected the caller should have made sure that
        # no more puts will be made, and the error will only be raised if something puts to
        # flag that it is wrong.
        if not self._allow_puts:
            raise WriteDisabledError()

        self._writer_queue.put_nowait(write_entry)

    def stop(self) -> None:
        if self._exit_when_empty:
            return

        self._allow_puts = False
        self._exit_when_empty = True

        # Wait for both threads to exit.
        self._writer_loop_event.wait()
        self._writer_thread.join()
        self._db_context.release_connection(self._db)
        self._db = None
        self._callback_loop_event.wait()
        self._callback_thread.join()

        self._is_alive = False

    def is_stopped(self) -> bool:
        return not self._is_alive


class DatabaseContext:
    MEMORY_PATH = ":memory:"

    def __init__(self, wallet_path: str) -> None:
        if not self.is_special_path(wallet_path) and not wallet_path.endswith(DATABASE_EXT):
            wallet_path += DATABASE_EXT
        self._db_path = wallet_path
        self._connections = []
        # self._debug_texts = {}

        self._write_dispatcher = SqliteWriteDispatcher(self)

    def acquire_connection(self) -> sqlite3.Connection:
        debug_text = traceback.format_stack()
        connection = sqlite3.connect(self._db_path, check_same_thread=False,
            isolation_level=None)
        connection.execute("PRAGMA foreign_keys = ON")
        # self._debug_texts[connection] = debug_text
        self._connections.append(connection)
        return connection

    def release_connection(self, connection: sqlite3.Connection) -> None:
        # del self._debug_texts[connection]
        self._connections.remove(connection)
        connection.close()

    def get_path(self) -> str:
        return self._db_path

    def queue_write(self, write_callback: WriteCallbackType,
            completion_callback: Optional[CompletionCallbackType]=None) -> None:
        self._write_dispatcher.put((write_callback, completion_callback))

    def close(self) -> None:
        self._write_dispatcher.stop()
        # for connection in self._connections:
        #     print(self._debug_texts[connection])
        assert self.is_closed(), f"{len(self._connections)}/{self._write_dispatcher.is_stopped()}"

    def is_closed(self) -> bool:
        return len(self._connections) == 0 and self._write_dispatcher.is_stopped()

    def is_special_path(self, path: str) -> bool:
        # Each connection has a private database.
        if path == self.MEMORY_PATH:
            return True
        # Shared temporary in-memory database.
        # file:memdb1?mode=memory&cache=shared"
        if path.startswith("file:") and "mode=memory" in path:
            return True
        return False

    @classmethod
    def shared_memory_uri(cls, unique_name: str) -> str:
        return f"file:{unique_name}?mode=memory&cache=shared"

class _QueryCompleter:
    def __init__(self):
        self._event = threading.Event()

        self._gave_callback = False
        self._have_result = False
        self._result: Any = None

    def get_callback(self) -> None:
        assert not self._gave_callback, "Query completer cannot be reused"
        def callback(exc_value: Any) -> None:
            self._have_result = True
            self._result = exc_value
            self._event.set()
        self._gave_callback = True
        return callback

    def succeeded(self) -> bool:
        if not self._have_result:
            self._event.wait()
        if self._result is None:
            return True
        exc_value = self._result
        self._result = None
        assert exc_value is not None
        raise exc_value # pylint: disable=raising-bad-type


class SynchronousWriter:
    def __init__(self):
        self._completer = _QueryCompleter()

    def __enter__(self):
        return self._completer

    def __exit__(self, type, value, traceback):
        pass
