from datetime import datetime, timedelta

from stockdata.windows import Window, chunk_backward, merge_intervals, plan_windows, subtract

D = datetime


def test_chunks_are_anchored_at_the_end_capped_and_contiguous():
    ws = chunk_backward(D(2024, 1, 1), D(2024, 6, 3), 89)
    assert ws[0].end == D(2024, 6, 3)           # newest first, anchored at the (exclusive) end
    assert ws[-1].start == D(2024, 1, 1)
    assert all(w.days <= 89 for w in ws)
    assert all(b.end == a.start for a, b in zip(ws, ws[1:]))  # no gaps, no overlap


def test_cap_holds_for_many_ranges():
    for days in (1, 88, 89, 90, 91, 178, 179, 1826):
        a = D(2020, 1, 1)
        ws = chunk_backward(a, a + timedelta(days=days), 89)
        assert max(w.days for w in ws) <= 89
        assert sum(w.days for w in ws) == days


def test_merge_and_subtract():
    assert merge_intervals([(D(2024, 1, 5), D(2024, 1, 10)), (D(2024, 1, 1), D(2024, 1, 5))]) == \
        [(D(2024, 1, 1), D(2024, 1, 10))]
    assert subtract([(D(2024, 1, 3), D(2024, 1, 5))], D(2024, 1, 1), D(2024, 1, 10)) == \
        [(D(2024, 1, 1), D(2024, 1, 3)), (D(2024, 1, 5), D(2024, 1, 10))]


def test_plan_covers_everything_when_nothing_is_covered():
    ws = plan_windows([], D(2024, 1, 1), D(2024, 6, 3), 89)
    assert ws[0].end == D(2024, 6, 3) and ws[-1].start == D(2024, 1, 1)


def test_plan_resumes_only_the_missing_part():
    covered = [(D(2024, 3, 15), D(2024, 6, 3))]
    ws = plan_windows(covered, D(2024, 1, 1), D(2024, 6, 3), 89)
    assert [(w.start, w.end) for w in ws] == [(D(2024, 1, 1), D(2024, 3, 15))]


def test_plan_is_empty_when_fully_covered():
    assert plan_windows([(D(2024, 1, 1), D(2024, 6, 3))], D(2024, 1, 1), D(2024, 6, 3), 89, 7) == []


def test_incremental_forward_window_overlaps_the_covered_edge():
    covered = [(D(2024, 1, 1), D(2024, 6, 3))]
    ws = plan_windows(covered, D(2024, 1, 1), D(2024, 6, 10), 89, overlap_days=7)
    assert ws == [Window(D(2024, 5, 27), D(2024, 6, 10))]


def test_no_overlap_added_for_a_first_run():
    ws = plan_windows([], D(2024, 1, 1), D(2024, 1, 20), 89, overlap_days=7)
    assert ws == [Window(D(2024, 1, 1), D(2024, 1, 20))]


def test_same_gap_gives_same_window_keys_so_retries_hit_the_same_checkpoint():
    covered = [(D(2024, 3, 1), D(2024, 4, 1)), (D(2024, 5, 1), D(2024, 6, 3))]
    first = plan_windows(covered, D(2024, 1, 1), D(2024, 6, 3), 89, 7)
    again = plan_windows(list(covered), D(2024, 1, 1), D(2024, 6, 3), 89, 7)
    assert first == again


def test_backfill_extends_downwards():
    covered = [(D(2024, 3, 1), D(2024, 6, 3))]
    ws = plan_windows(covered, D(2023, 1, 1), D(2024, 6, 3), 89)
    assert ws[0].end == D(2024, 3, 1) and ws[-1].start == D(2023, 1, 1)
