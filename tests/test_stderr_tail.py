"""Unit tests for M6.6 StderrTail ring buffer."""

import threading

from agent.backends.stderr_tail import StderrTail


class TestStderrTail:
    """Tests for the fixed-size ring buffer used to capture stderr tails."""

    def test_empty_tail(self):
        """A brand-new buffer returns an empty string from tail()."""
        buf = StderrTail()
        assert buf.tail() == ""

    def test_write_and_tail(self):
        """write() stores data and tail() returns it."""
        buf = StderrTail()
        buf.write("error: something went wrong\n")
        assert buf.tail() == "error: something went wrong\n"

    def test_overflow_discards_oldest(self):
        """Writing more than max_bytes discards the oldest chunks first."""
        buf = StderrTail(max_bytes=32)
        buf.write("AAAA")  # 4 bytes
        buf.write("BBBB")  # 4 bytes -> total 8
        buf.write("CCCC")  # 4 bytes -> total 12
        buf.write("DDDD")  # 4 bytes -> total 16
        buf.write("EEEE")  # 4 bytes -> total 20
        # 32-byte capacity, write 24 more bytes -> overflow
        buf.write("X" * 24)  # total would be 44 -> trim oldest until <= 32
        result = buf.tail()
        # Oldest chunks (AAAA, BBBB, CCCC) should be discarded.
        # Remaining: DDDD (4) + EEEE (4) + X*24 (24) = 32 bytes
        assert "AAAA" not in result
        assert "BBBB" not in result
        assert "CCCC" not in result
        assert "DDDD" in result
        assert "EEEE" in result
        assert "X" * 24 in result

    def test_max_bytes_default(self):
        """Default max_bytes is 2048."""
        buf = StderrTail()
        assert buf.max_size == 2048

    def test_custom_max_bytes(self):
        """A custom max_bytes can be set via the constructor."""
        buf = StderrTail(max_bytes=512)
        assert buf.max_size == 512

    def test_clear_resets_buffer(self):
        """clear() empties the buffer completely."""
        buf = StderrTail()
        buf.write("some data")
        assert buf.tail() == "some data"
        buf.clear()
        assert buf.tail() == ""
        assert buf.size == 0

    def test_size_property(self):
        """size tracks the current content length in bytes/characters."""
        buf = StderrTail()
        assert buf.size == 0
        buf.write("hello")
        assert buf.size == 5
        buf.write(" world")
        assert buf.size == 11

    def test_multiline_content(self):
        """Handles multiline stderr output correctly."""
        buf = StderrTail()
        buf.write("line1\n")
        buf.write("line2\n")
        buf.write("line3\n")
        assert buf.tail() == "line1\nline2\nline3\n"

    def test_unicode_content(self):
        """Handles unicode characters without errors."""
        buf = StderrTail()
        buf.write("erreur: données invalides ✘\n")
        assert buf.tail() == "erreur: données invalides ✘\n"

    def test_exact_boundary(self):
        """Writing exactly max_bytes keeps everything, nothing is discarded."""
        buf = StderrTail(max_bytes=10)
        buf.write("12345")
        buf.write("67890")
        assert buf.size == 10
        assert buf.tail() == "1234567890"

    def test_thread_safety(self):
        """Concurrent writes from multiple threads do not corrupt the buffer."""
        buf = StderrTail(max_bytes=2048)
        n_threads = 10
        writes_per_thread = 100
        errors: list[Exception] = []

        def writer(thread_id: int) -> None:
            try:
                for i in range(writes_per_thread):
                    buf.write(f"t{thread_id}-{i}\n")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer, args=(tid,)) for tid in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert errors == []
        # Total writes = n_threads * writes_per_thread lines
        # Each line like "t0-0\n" = at least 5 chars -> total > 2048, so some trimmed
        lines = buf.tail().split("\n")
        # All remaining lines should be well-formed (no corruption)
        for line in lines:
            if line:
                assert line.startswith("t"), f"Corrupted line: {line!r}"
