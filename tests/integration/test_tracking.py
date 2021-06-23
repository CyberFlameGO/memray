import collections
import datetime
import mmap
import signal
import subprocess
import sys
import textwrap
import threading
from pathlib import Path

import pytest

from bloomberg.pensieve import AllocatorType
from bloomberg.pensieve import Tracker
from bloomberg.pensieve._pensieve import MmapAllocator
from bloomberg.pensieve._test import MemoryAllocator
from tests.utils import filter_relevant_allocations

ALLOCATORS = [
    ("malloc", AllocatorType.MALLOC),
    ("valloc", AllocatorType.VALLOC),
    ("pvalloc", AllocatorType.PVALLOC),
    ("calloc", AllocatorType.CALLOC),
    ("memalign", AllocatorType.MEMALIGN),
    ("posix_memalign", AllocatorType.POSIX_MEMALIGN),
    ("realloc", AllocatorType.REALLOC),
]

PAGE_SIZE = mmap.PAGESIZE


def test_no_allocations_while_tracking(tmp_path):
    with Tracker(tmp_path / "test.bin") as tracker:
        pass

    assert list(tracker.reader.get_allocation_records()) == []


@pytest.mark.parametrize(["allocator_func", "allocator_type"], ALLOCATORS)
def test_simple_allocation_tracking(allocator_func, allocator_type, tmp_path):
    # GIVEN
    allocator = MemoryAllocator()

    # WHEN
    with Tracker(tmp_path / "test.bin") as tracker:
        getattr(allocator, allocator_func)(1234)
        allocator.free()

    # THEN
    allocations = list(tracker.reader.get_allocation_records())
    allocs = [
        event
        for event in allocations
        if event.size == 1234 and event.allocator == allocator_type
    ]
    assert len(allocs) == 1
    (alloc,) = allocs

    frees = [
        event
        for event in allocations
        if event.address == alloc.address and event.allocator == AllocatorType.FREE
    ]
    assert len(frees) >= 1


def test_mmap_tracking(tmp_path):
    # GIVEN / WHEN
    with Tracker(tmp_path / "test.bin") as tracker:
        with mmap.mmap(-1, length=2048, access=mmap.ACCESS_WRITE) as mmap_obj:
            mmap_obj[0:100] = b"a" * 100

    # THEN
    records = list(tracker.reader.get_allocation_records())
    assert len(records) >= 2

    mmap_records = [
        record
        for record in records
        if AllocatorType.MMAP == record.allocator and record.size == 2048
    ]
    assert len(mmap_records) == 1
    mmunmap_record = [
        record for record in records if AllocatorType.MUNMAP == record.allocator
    ]
    assert len(mmunmap_record) == 1


def test_pthread_tracking(tmp_path):
    # GIVEN
    allocator = MemoryAllocator()

    def tracking_function():
        allocator.valloc(1234)
        allocator.free()

    # WHEN
    with Tracker(tmp_path / "test.bin") as tracker:
        allocator.run_in_pthread(tracking_function)

    # THEN
    allocations = list(tracker.reader.get_allocation_records())
    allocs = [
        event
        for event in allocations
        if event.size == 1234 and event.allocator == AllocatorType.VALLOC
    ]
    assert len(allocs) == 1
    (alloc,) = allocs

    frees = [
        event
        for event in allocations
        if event.address == alloc.address and event.allocator == AllocatorType.FREE
    ]
    assert len(frees) >= 1


def test_tracking_with_SIGKILL(tmpdir):
    """Verify that we can successfully retrieve the allocations after SIGKILL."""
    # GIVEN
    output = Path(tmpdir) / "test.bin"
    subprocess_code = textwrap.dedent(
        f"""
        import os
        import signal
        from bloomberg.pensieve import Tracker
        from bloomberg.pensieve._test import MemoryAllocator

        allocator = MemoryAllocator()
        output = "{output}"

        with Tracker(output) as tracker:
            allocator.valloc(1024)
            os.kill(os.getpid(), signal.SIGKILL)
    """
    )

    # WHEN
    process = subprocess.run([sys.executable, "-c", subprocess_code])

    # THEN
    assert process.returncode == -signal.SIGKILL

    tracker = Tracker(output)
    records = list(tracker.reader.get_allocation_records())
    vallocs = [
        record
        for record in filter_relevant_allocations(records)
        if record.allocator == AllocatorType.VALLOC
    ]
    (allocation,) = vallocs
    assert allocation.size == 1024


class TestHighWatermark:
    def test_no_allocations_while_tracking(self, tmp_path):
        # GIVEN / WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            pass

        # THEN
        assert list(tracker.reader.get_high_watermark_allocation_records()) == []

    @pytest.mark.parametrize(["allocator_func", "allocator_type"], ALLOCATORS)
    def test_simple_allocation_tracking(self, tmp_path, allocator_func, allocator_type):
        # GIVEN
        allocator = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            getattr(allocator, allocator_func)(1234)
            allocator.free()

        # THEN
        peak_allocations_unfiltered = (
            tracker.reader.get_high_watermark_allocation_records()
        )
        peak_allocations = [
            record for record in peak_allocations_unfiltered if record.size == 1234
        ]
        assert len(peak_allocations) == 1, peak_allocations

        record = peak_allocations[0]
        assert record.allocator == allocator_type
        assert record.n_allocations == 1

    def test_multiple_high_watermark(self, tmp_path):
        # GIVEN
        allocator = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            for _ in range(2):
                allocator.valloc(1024)
                allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 4

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 1
        record = peak_allocations[0]

        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 1024
        assert record.n_allocations == 1

    def test_freed_before_high_watermark_do_not_appear(self, tmp_path):
        # GIVEN
        allocator1 = MemoryAllocator()
        allocator2 = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator1.valloc(1024)
            allocator1.free()
            allocator2.valloc(2048)
            allocator2.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 4

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 1

        record = peak_allocations[0]
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 2048
        assert record.n_allocations == 1

    def test_freed_after_high_watermark_do_not_appear(self, tmp_path):
        # GIVEN
        allocator1 = MemoryAllocator()
        allocator2 = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator2.valloc(2048)
            allocator2.free()
            allocator1.valloc(1024)
            allocator1.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 4

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 1

        record = peak_allocations[0]
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 2048
        assert record.n_allocations == 1

    def test_allocations_aggregation_on_same_line(self, tmp_path):
        # GIVEN
        allocators = []

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            for _ in range(2):
                allocator = MemoryAllocator()
                allocators.append(allocator)

                allocator.valloc(1024)

            for allocator in allocators:
                allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 4

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 1

        record = peak_allocations[0]
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 2048
        assert record.n_allocations == 2

    def test_allocations_aggregation_on_different_lines(self, tmp_path):
        # GIVEN
        allocator1 = MemoryAllocator()
        allocator2 = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator1.valloc(1024)
            allocator2.valloc(2048)
            allocator1.free()
            allocator2.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 4

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 2
        assert sum(record.size for record in peak_allocations) == 1024 + 2048
        assert all(record.n_allocations == 1 for record in peak_allocations)

    def test_non_freed_allocations_are_accounted_for(self, tmp_path):
        # GIVEN
        allocator1 = MemoryAllocator()
        allocator2 = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator1.valloc(1024)
            allocator2.valloc(2048)
            allocator1.free()
            allocator2.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 4

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 2
        assert sum(record.size for record in peak_allocations) == 1024 + 2048
        assert all(record.n_allocations == 1 for record in peak_allocations)

    def test_final_allocation_is_peak(self, tmp_path):
        # GIVEN
        allocator1 = MemoryAllocator()
        allocator2 = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator1.valloc(1024)
            allocator1.free()
            allocator2.valloc(2048)
        allocator2.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 3

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 1

        record = peak_allocations[0]
        assert record.n_allocations == 1
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 2048

    def test_spiky_generally_increasing_to_final_peak(self, tmp_path):
        """Checks multiple aspects with an interesting toy function."""

        # GIVEN
        def recursive(n, chunk_size):
            """Mimics generally-increasing but spiky usage"""
            if not n:
                return

            allocator = MemoryAllocator()
            print(f"+{n:>2} kB")
            allocator.valloc(n * chunk_size)

            # Don't keep allocated memory when recursing, ~50% of the calls.
            if n % 2:
                allocator.free()
                print(f"-{n:>2} kB")
                recursive(n - 1, chunk_size)
            else:
                recursive(n - 1, chunk_size)
                allocator.free()
                print(f"-{n:>2} kB")

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            recursive(10, 1024)

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 20
        assert sum(record.size for record in all_allocations) == 56320

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert all(record.n_allocations == 1 for record in peak_allocations)

        expected = {10, 8, 6, 4, 2, 1}
        assert len(peak_allocations) == len(expected)
        assert {record.size / 1024 for record in peak_allocations} == expected

    def test_allocations_after_high_watermark_is_freed_do_not_appear(self, tmp_path):
        # GIVEN
        allocator = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator.valloc(2048)
            allocator.free()
            allocator.valloc(1024)
        allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 3

        peak_allocations = list(
            filter_relevant_allocations(reader.get_high_watermark_allocation_records())
        )
        assert len(peak_allocations) == 1

        record = peak_allocations[0]
        assert record.n_allocations == 1
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 2048

    def test_partial_munmap(self, tmp_path):
        """Partial munmap operations should be accurately tracked: we should
        only account for the removal of the actually munmap'd chunk and not
        the entire mmap'd region when a partial munmap is performed."""

        # GIVEN/WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            # Mmap some memory and free just the first page. This should not register
            # the deallocation of the entire mmap'd region, only one page.
            alloc = MmapAllocator(2 * PAGE_SIZE)
            alloc.munmap(PAGE_SIZE)

            # Now perform the peak allocation. This should be detected as the peak index.
            MmapAllocator(10 * PAGE_SIZE)
            # At this point we should have 11 * PAGE_SIZE allocated

        # THEN
        reader = tracker.reader
        peak_allocations = list(reader.get_high_watermark_allocation_records())
        assert len(peak_allocations) == 2
        peak_memory = sum(x.size for x in peak_allocations)
        assert peak_memory == 11 * PAGE_SIZE

    def test_partial_munmap_gap(self, tmp_path):
        """Validate that removing chunks from a mmap'd region correctly accounts
        for the parts removed. This test allocates 4 pages and removes the first
        and last pages of the mmap'd region."""

        # GIVEN/WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            # Mmap some memory and free two pages: one at the beginning and one at the
            # end of the region.
            alloc = MmapAllocator(4 * PAGE_SIZE)
            alloc.munmap(PAGE_SIZE)
            alloc.munmap(PAGE_SIZE, 3 * PAGE_SIZE)

            # Now perform the peak allocation. This should be detected as the peak index.
            MmapAllocator(10 * PAGE_SIZE)
            # At this point we should have 12 * PAGE_SIZE allocated

        # THEN
        reader = tracker.reader
        peak_allocations = list(reader.get_high_watermark_allocation_records())
        assert len(peak_allocations) == 2
        peak_memory = sum(x.size for x in peak_allocations)
        assert peak_memory == 12 * PAGE_SIZE

    def test_munmap_multiple_mmaps(self, tmp_path):
        """Allocate multiple contiguous mmap'd regions and then deallocate all of them
        with munmap in one go."""

        # GIVEN
        # Ensure we have a long enough free buffer for the contiguous mmap's. We also
        # need to make sure the allocation addresses are page-aligned later, and mmap
        # does that # for us. We can then use the pointer from this allocation for the
        # actual test.
        buf = MmapAllocator(8 * PAGE_SIZE)
        buf.munmap(8 * PAGE_SIZE)

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            # Allocate 2 contiguous chunks of 4 pages (8 pages in total) and free them
            # with a single munmap
            alloc1 = MmapAllocator(4 * PAGE_SIZE, buf.address)
            MmapAllocator(4 * PAGE_SIZE, alloc1.address + (4 * PAGE_SIZE))
            alloc1.munmap(8 * PAGE_SIZE)

            # Now perform the peak allocation. This should be detected as the peak index.
            MmapAllocator(10 * PAGE_SIZE)

        # THEN
        reader = tracker.reader
        peak_allocations = list(
            filter_relevant_allocations(
                reader.get_high_watermark_allocation_records(), ranged=True
            )
        )
        peak_memory = sum(x.size for x in peak_allocations)
        assert peak_memory == 10 * PAGE_SIZE

    def test_munmap_multiple_mmaps_multiple_munmaps(self, tmp_path):
        """Allocate multiple contiguous mmap'd regions and then with multiple munmap's, each
        deallocating several mmap'd areas in one go."""
        # GIVEN
        # Ensure we have a long enough free buffer for the contiguous mmap's. We also
        # need to make sure the allocation addresses are page-aligned later, and mmap does that
        # for us. We can then use the pointer from this allocation for the actual test.
        buf = MmapAllocator(8 * PAGE_SIZE)
        buf.munmap(8 * PAGE_SIZE)

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            alloc1 = MmapAllocator(2 * PAGE_SIZE, buf.address)
            MmapAllocator(2 * PAGE_SIZE, buf.address + (2 * PAGE_SIZE))
            MmapAllocator(2 * PAGE_SIZE, buf.address + (4 * PAGE_SIZE))
            MmapAllocator(2 * PAGE_SIZE, buf.address + (6 * PAGE_SIZE))
            alloc1.munmap(4 * PAGE_SIZE)
            alloc1.munmap(4 * PAGE_SIZE, 4 * PAGE_SIZE)

            # Now perform the peak allocation. This should be detected as the peak index.
            MmapAllocator(10 * PAGE_SIZE)

        # THEN
        reader = tracker.reader
        peak_allocations = list(
            filter_relevant_allocations(
                reader.get_high_watermark_allocation_records(), ranged=True
            )
        )
        peak_memory = sum(x.size for x in peak_allocations)
        assert peak_memory == 10 * PAGE_SIZE

    def test_partial_munmap_multiple_split_in_middle(self, tmp_path):
        """Deallocate pages in of a larger mmap'd area, splitting it into 3 areas."""
        # GIVEN/WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            alloc = MmapAllocator(5 * PAGE_SIZE)
            alloc.munmap(PAGE_SIZE, 1 * PAGE_SIZE)
            alloc.munmap(PAGE_SIZE, 3 * PAGE_SIZE)

            MmapAllocator(10 * PAGE_SIZE)

        # THEN
        reader = tracker.reader
        peak_allocations = list(reader.get_high_watermark_allocation_records())
        assert len(peak_allocations) == 2
        peak_memory = sum(x.size for x in peak_allocations)
        assert peak_memory == 13 * PAGE_SIZE

    def test_partial_munmap_split_in_middle(self, tmp_path):
        """Deallocate a single page in the middle of a larger mmap'd area."""
        # GIVEN/WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            alloc = MmapAllocator(8 * PAGE_SIZE)
            alloc.munmap(PAGE_SIZE, 4 * PAGE_SIZE)

            MmapAllocator(10 * PAGE_SIZE)

        # THEN
        reader = tracker.reader
        peak_allocations = list(reader.get_high_watermark_allocation_records())
        assert len(peak_allocations) == 2
        peak_memory = sum(x.size for x in peak_allocations)
        assert peak_memory == 17 * PAGE_SIZE


class TestLeaks:
    def test_leaks_allocations_are_detected(self, tmp_path):
        # GIVEN
        allocator = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator.valloc(1024)
        allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 1

        leaked_allocations = list(
            filter_relevant_allocations(reader.get_leaked_allocation_records())
        )
        assert len(leaked_allocations) == 1

        record = leaked_allocations[0]
        assert record.n_allocations == 1
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 1024

    def test_allocations_that_are_freed_do_not_appear_as_leaks(self, tmp_path):
        # GIVEN
        allocator = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator.valloc(1024)
            allocator.free()
            allocator.valloc(1024)
            allocator.free()
            allocator.valloc(1024)
            allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 6

        leaked_allocations = list(
            filter_relevant_allocations(reader.get_leaked_allocation_records())
        )
        assert not leaked_allocations

    def test_leak_that_happens_in_the_middle_is_detected(self, tmp_path):
        # GIVEN
        allocator = MemoryAllocator()
        leak_allocator = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator.valloc(1024)
            allocator.free()
            allocator.valloc(1024)
            leak_allocator.valloc(2048)
            allocator.free()
            allocator.valloc(1024)
            allocator.free()
        leak_allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 7

        leaked_allocations = list(
            filter_relevant_allocations(reader.get_leaked_allocation_records())
        )

        assert len(leaked_allocations) == 1

        record = leaked_allocations[0]
        assert record.n_allocations == 1
        assert record.allocator == AllocatorType.VALLOC
        assert record.size == 2048

    def test_leaks_that_happens_in_different_lines(self, tmp_path):
        # GIVEN
        allocator1 = MemoryAllocator()
        allocator2 = MemoryAllocator()

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator1.valloc(1024)
            allocator2.valloc(2048)

        allocator1.free()
        allocator2.free()

        # THEN
        leaked_allocations = list(
            filter_relevant_allocations(tracker.reader.get_leaked_allocation_records())
        )
        assert len(leaked_allocations) == 2
        assert sum(record.size for record in leaked_allocations) == 1024 + 2048
        assert all(record.n_allocations == 1 for record in leaked_allocations)

    def test_leaks_that_happen_in_the_same_function_are_aggregated(self, tmp_path):

        # GIVEN
        allocators = []

        def foo():
            allocator = MemoryAllocator()
            allocator.valloc(1024)
            allocators.append(allocator)

        # WHEN
        with Tracker(tmp_path / "test.bin") as tracker:
            for _ in range(10):
                foo()

        for allocator in allocators:
            allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(
            filter_relevant_allocations(reader.get_allocation_records())
        )
        assert len(all_allocations) == 10

        leaked_allocations = list(
            filter_relevant_allocations(reader.get_leaked_allocation_records())
        )
        assert len(leaked_allocations) == 1
        (allocation,) = leaked_allocations
        assert allocation.size == 1024 * 10
        assert allocation.n_allocations == 10

    def test_unmatched_deallocations_are_not_reported(self, tmp_path):
        # GIVEN
        allocator = MemoryAllocator()

        # WHEN
        allocator.valloc(1234)
        with Tracker(tmp_path / "test.bin") as tracker:
            allocator.free()

        # THEN
        reader = tracker.reader
        all_allocations = list(reader.get_allocation_records())
        assert len(all_allocations) >= 1
        assert not list(
            filter_relevant_allocations(reader.get_leaked_allocation_records())
        )

    def test_thread_allocations_multiple_threads(self, tmpdir):

        # GIVEN
        def allocating_function(allocator, amount, stop_flag):
            allocator.posix_memalign(amount)
            allocator.posix_memalign(amount)
            # We need a barrier as pthread might reuse the same thread ID if the first thread
            # finishes before the other one starts
            stop_flag.wait()

        # WHEN
        alloc1 = MemoryAllocator()
        stop_flag1 = threading.Event()
        alloc2 = MemoryAllocator()
        stop_flag2 = threading.Event()
        with Tracker(Path(tmpdir) / "test.bin") as tracker:
            t1 = threading.Thread(
                target=allocating_function, args=(alloc1, 2048, stop_flag1)
            )
            t1.start()
            t2 = threading.Thread(
                target=allocating_function, args=(alloc2, 2048, stop_flag2)
            )
            t2.start()

            stop_flag1.set()
            t1.join()
            stop_flag2.set()
            t2.join()

        # THEN
        reader = tracker.reader
        all_allocations = [
            record
            for record in reader.get_allocation_records()
            if record.allocator == AllocatorType.POSIX_MEMALIGN
        ]
        assert len(all_allocations) == 4

        high_watermark_records = (
            record
            for record in reader.get_high_watermark_allocation_records(
                merge_threads=False
            )
            if record.allocator == AllocatorType.POSIX_MEMALIGN
        )
        # Group the allocations per thread
        records = collections.defaultdict(list)
        for record in high_watermark_records:
            records[record.tid].append(record)
        assert len(records.keys()) == 2
        # Each thread should have 4096 bytes total allocations
        for tid, allocations in records.items():
            assert sum(allocation.size for allocation in allocations) == 4096


class TestHeader:
    def test_get_header(self, monkeypatch, tmpdir):
        # GIVEN
        allocator = MemoryAllocator()
        output = Path(tmpdir) / "test.bin"

        # WHEN

        monkeypatch.setattr(sys, "argv", ["python", "-m", "pytest"])
        start_time = datetime.datetime.now()
        with Tracker(output) as tracker:
            for _ in range(100):
                allocator.valloc(1024)
        end_time = datetime.datetime.now()

        n_records = len(list(tracker.reader.get_allocation_records()))
        metadata = tracker.reader.metadata

        # THEN
        assert metadata.end_time > metadata.start_time
        assert abs(metadata.start_time - start_time).seconds < 1
        assert abs(metadata.end_time - end_time).seconds < 1
        assert metadata.total_allocations == n_records
        assert metadata.command_line == "python -m pytest"
        assert metadata.peak_memory == 1024 * 100

    def test_get_header_after_snapshot(self, monkeypatch, tmpdir):
        """Verify that we can successfully retrieve the metadata after querying
        the high watermark snapshot."""
        # GIVEN
        allocator = MemoryAllocator()
        output = Path(tmpdir) / "test.bin"

        # WHEN

        monkeypatch.setattr(sys, "argv", ["python", "-m", "pytest"])
        start_time = datetime.datetime.now()
        with Tracker(output) as tracker:
            for _ in range(100):
                allocator.valloc(1024)
        end_time = datetime.datetime.now()

        peak, *_ = list(tracker.reader.get_high_watermark_allocation_records())
        metadata = tracker.reader.metadata

        # THEN
        assert metadata.end_time > metadata.start_time
        assert abs(metadata.start_time - start_time).seconds < 1
        assert abs(metadata.end_time - end_time).seconds < 1
        assert metadata.total_allocations == peak.n_allocations
        assert metadata.command_line == "python -m pytest"
        assert metadata.peak_memory == 1024 * 100
