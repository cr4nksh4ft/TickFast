import argparse
import asyncio
import json
import logging
import math
import multiprocessing
import random
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from peewee import PeeweeException

from models.basemodel import DatabaseConfigurationError, get_database
from scripts.credentials import BurstUser, prepare_credentials, read_user_ids
from tickfast.api.auth import AuthConfigurationError
from utils.env import env

DEFAULT_SEATS = ("A1", "A2", "A3", "A4")
DEFAULT_REQUESTS = 20_000
DEFAULT_CONCURRENCY = 500
DEFAULT_PROCESSES = 4
LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class Attempt:
    user: BurstUser
    seat: str
    idempotency_key: str
    is_retry: bool = False


@dataclass(frozen=True)
class AttemptResult:
    attempt: Attempt
    status_code: int | None
    body: object = None
    error: str | None = None
    elapsed_ms: float | None = None
    request_index: int | None = None
    request_id: str | None = None
    started_offset_seconds: float | None = None
    completed_offset_seconds: float | None = None
    retry_attempts: int = 0


def build_attempts(
    users: list[BurstUser],
    seats: list[str],
    request_count: int,
    retry_percent: float,
    seed: int,
) -> list[Attempt]:
    retry_count = round(request_count * retry_percent / 100)
    unique_count = request_count - retry_count
    if unique_count < len(seats) or retry_count > unique_count:
        raise ValueError("request count must cover every seat and its retry count")

    originals = [
        Attempt(
            user=users[index % len(users)],
            seat=seats[index % len(seats)],
            idempotency_key=f"burst-{uuid4().hex}",
        )
        for index in range(unique_count)
    ]
    generator = random.Random(seed)
    retried_indexes = generator.sample(range(unique_count), retry_count)
    attempts = originals + [
        replace(originals[index], is_retry=True) for index in retried_indexes
    ]
    generator.shuffle(attempts)
    return attempts


def _stable_response(result: AttemptResult) -> tuple[int | None, str]:
    body = result.body
    if isinstance(body, dict):
        body = body.copy()
        detail = body.get("detail")
        if isinstance(detail, dict):
            detail = detail.copy()
            detail.pop("request_id", None)
            body["detail"] = detail
    return (
        result.status_code,
        json.dumps(body, sort_keys=True, separators=(",", ":"), default=str),
    )


def _read_mysql_snapshot() -> dict[str, object]:
    database = get_database()
    with database.connection_context():
        status_rows = database.execute_sql(
            """
            SHOW GLOBAL STATUS WHERE Variable_name IN (
                'Threads_connected', 'Threads_running', 'Max_used_connections',
                'Aborted_connects'
            )
            """
        ).fetchall()
        variables = database.execute_sql(
            "SHOW GLOBAL VARIABLES WHERE Variable_name = 'max_connections'"
        ).fetchall()
        statuses = {str(name): int(value) for name, value in status_rows}
        max_connections = int(variables[0][1]) if variables else None
        try:
            lock_waits = int(
                database.execute_sql(
                    "SELECT COUNT(*) FROM performance_schema.data_lock_waits"
                ).fetchone()[0]
            )
            lock_wait_error = None
        except PeeweeException as exc:
            lock_waits = None
            lock_wait_error = type(exc).__name__
    return {
        "statuses": statuses,
        "max_connections": max_connections,
        "current_innodb_lock_waits": lock_waits,
        "lock_wait_sample_error": lock_wait_error,
    }


async def _sample_mysql(
    stop_event: asyncio.Event,
    interval_seconds: float,
    started_at: float,
) -> list[dict[str, object]]:
    samples = []
    while True:
        try:
            sample = await asyncio.to_thread(_read_mysql_snapshot)
            event = {"event": "mysql_sample", **sample}
        except (PeeweeException, DatabaseConfigurationError, OSError) as exc:
            event = {
                "event": "mysql_sample",
                "sample_error": type(exc).__name__,
            }
        event["elapsed_seconds"] = round(time.perf_counter() - started_at, 3)
        samples.append(event)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
            return samples
        except TimeoutError:
            pass


def _latency_summary(samples: list[float]) -> dict[str, float | int]:
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0}

    def percentile(value: float) -> float:
        index = max(0, math.ceil(len(ordered) * value) - 1)
        return round(ordered[index], 2)

    return {
        "count": len(ordered),
        "mean_ms": round(sum(ordered) / len(ordered), 2),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": round(ordered[-1], 2),
    }


def _write_metrics(
    path: Path,
    run_id: str,
    arguments: argparse.Namespace,
    results: list[AttemptResult],
    mysql_samples: list[dict[str, object]],
    peak_in_flight: int,
    elapsed_seconds: float,
    final_state: dict[str, object],
    exit_code: int,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    latencies = [
        result.elapsed_ms for result in results if result.elapsed_ms is not None
    ]
    latency_by_status: dict[str, list[float]] = defaultdict(list)
    status_counts = Counter()
    transport_errors = Counter()
    for result in results:
        if result.error:
            transport_errors[result.error] += 1
        else:
            status_counts[str(result.status_code)] += 1
        if result.elapsed_ms is not None:
            key = str(result.status_code) if result.status_code is not None else result.error or "error"
            latency_by_status[key].append(result.elapsed_ms)

    counts = final_state.get("counts", {})
    total_seats = final_state.get("total_seats")
    count_sum = sum(int(value) for value in counts.values()) if isinstance(counts, dict) else -1
    reconciled = (
        isinstance(counts, dict)
        and isinstance(total_seats, int)
        and count_sum == total_seats
        and int(counts.get("held", -1)) == 0
    )
    now = datetime.now(UTC).isoformat()
    with path.open("a", encoding="utf-8") as metrics_file:
        for result in results:
            body = result.body
            detail = body.get("detail") if isinstance(body, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            metrics_file.write(
                json.dumps(
                    {
                        "event": "request",
                        "run_id": run_id,
                        "request_index": result.request_index,
                        "request_id": result.request_id,
                        "seat": result.attempt.seat,
                        "idempotency_key": result.attempt.idempotency_key,
                        "planned_retry": result.attempt.is_retry,
                        "retry_attempts": result.retry_attempts,
                        "status_code": result.status_code,
                        "decline_code": code,
                        "transport_error": result.error,
                        "elapsed_ms": result.elapsed_ms,
                        "started_offset_seconds": result.started_offset_seconds,
                        "completed_offset_seconds": result.completed_offset_seconds,
                    },
                    separators=(",", ":"),
                )
                + "\n"
            )
        for sample in mysql_samples:
            metrics_file.write(
                json.dumps({**sample, "run_id": run_id}, separators=(",", ":"))
                + "\n"
            )
        summary = {
            "event": "run_summary",
            "run_id": run_id,
            "timestamp": now,
            "requests": arguments.requests,
            "concurrency_cap": arguments.concurrency,
            "processes": arguments.processes,
            "users": arguments.users,
            "retry_percent": arguments.retry_percent,
            "retry_deadline_seconds": arguments.retry_deadline,
            "retry_attempts": sum(result.retry_attempts for result in results),
            "recovery_successes": sum(
                result.retry_attempts > 0 and result.status_code == 201
                for result in results
            ),
            "duration_seconds": round(elapsed_seconds, 3),
            "requests_per_second": round(len(results) / elapsed_seconds, 2)
            if elapsed_seconds
            else 0.0,
            "peak_in_flight": peak_in_flight,
            "status_counts": dict(status_counts),
            "transport_errors": dict(transport_errors),
            "latency_ms": _latency_summary(latencies),
            "latency_by_status_ms": {
                key: _latency_summary(values)
                for key, values in latency_by_status.items()
            },
            "mysql_sample_count": len(mysql_samples),
            "final_seat_counts": counts,
            "final_total_seats": total_seats,
            "reconciliation_passed": reconciled,
            "burst_exit_code": exit_code,
        }
        metrics_file.write(
            json.dumps(summary, separators=(",", ":")) + "\n"
        )


async def send_attempts(
    client: httpx.AsyncClient,
    show_id: int,
    attempts: list[Attempt],
    concurrency: int,
    retry_deadline_seconds: float = 60.0,
    max_retries: int | None = None,
    started_at: float | None = None,
    first_index: int = 1,
) -> tuple[list[AttemptResult], int]:
    semaphore = asyncio.Semaphore(concurrency)
    in_flight = 0
    peak_in_flight = 0
    completed = 0
    if started_at is None:
        started_at = time.perf_counter()
    progress_interval = max(1, (len(attempts) + 19) // 20)

    async def send(index: int, attempt: Attempt) -> AttemptResult:
        nonlocal in_flight, peak_in_flight, completed

        def mark_completed() -> None:
            nonlocal in_flight, completed
            in_flight -= 1
            completed += 1
            if completed % progress_interval == 0 or completed == len(attempts):
                elapsed = time.perf_counter() - started_at
                LOGGER.info(
                    "Progress: %d/%d requests completed (%.1f req/s; "
                    "%d in flight)",
                    completed,
                    len(attempts),
                    completed / elapsed if elapsed else 0.0,
                    in_flight,
                )

        async with semaphore:
            in_flight += 1
            peak_in_flight = max(peak_in_flight, in_flight)
            request_started_at = time.perf_counter()
            started_offset_seconds = request_started_at - started_at
            retry_attempts = 0
            retry_deadline = asyncio.get_running_loop().time() + retry_deadline_seconds
            while True:
                remaining = retry_deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    mark_completed()
                    return AttemptResult(
                        attempt,
                        None,
                        error="RetryDeadlineExceeded",
                        elapsed_ms=(time.perf_counter() - request_started_at) * 1000,
                        request_index=index,
                        retry_attempts=retry_attempts,
                        started_offset_seconds=started_offset_seconds,
                        completed_offset_seconds=time.perf_counter() - started_at,
                    )
                try:
                    async with asyncio.timeout(remaining):
                        response = await client.post(
                            f"/shows/{show_id}/reserve",
                            headers={
                                "Authorization": f"Bearer {attempt.user.token}",
                                "Idempotency-Key": attempt.idempotency_key,
                            },
                            json={"seats": [attempt.seat]},
                        )
                except (httpx.HTTPError, TimeoutError) as exc:
                    if max_retries is not None and retry_attempts >= max_retries:
                        elapsed_ms = (time.perf_counter() - request_started_at) * 1000
                        LOGGER.debug(
                            "Transport failure request=%d/%d user_id=%d seat=%s "
                            "idempotency_key=%s retries=%d: %s: %s",
                            index,
                            len(attempts),
                            attempt.user.user_id,
                            attempt.seat,
                            attempt.idempotency_key,
                            retry_attempts,
                            type(exc).__name__,
                            exc,
                            exc_info=True,
                        )
                        mark_completed()
                        return AttemptResult(
                            attempt,
                            None,
                            error=type(exc).__name__,
                            elapsed_ms=elapsed_ms,
                            request_index=index,
                            retry_attempts=retry_attempts,
                            started_offset_seconds=started_offset_seconds,
                            completed_offset_seconds=time.perf_counter() - started_at,
                        )
                    remaining = retry_deadline - asyncio.get_running_loop().time()
                    if remaining <= 0:
                        continue
                    await asyncio.sleep(
                        _retry_delay_seconds(retry_attempts, remaining)
                    )
                    retry_attempts += 1
                    continue
                try:
                    body = response.json()
                except ValueError:
                    body = {"response_text": response.text[:300]}
                detail = body.get("detail") if isinstance(body, dict) else None
                code = detail.get("code") if isinstance(detail, dict) else None
                retryable_response = code in {
                    "hold_in_progress",
                    "reservation_retry",
                    "reservation_unavailable",
                } or response.status_code == 503
                if retryable_response and (
                    max_retries is None or retry_attempts < max_retries
                ):
                    remaining = retry_deadline - asyncio.get_running_loop().time()
                    if remaining > 0:
                        retry_after_ms = (
                            detail.get("retry_after_ms")
                            if isinstance(detail, dict)
                            else None
                        )
                        delay_seconds = _retry_delay_seconds(
                            retry_attempts,
                            remaining,
                            retry_after_ms=retry_after_ms,
                            retry_after_header=response.headers.get("Retry-After"),
                        )
                        if delay_seconds < remaining:
                            await asyncio.sleep(delay_seconds)
                            retry_attempts += 1
                            continue
                elapsed_ms = (time.perf_counter() - request_started_at) * 1000
                if response.status_code not in (201, 409):
                    LOGGER.debug(
                        "Unexpected response request=%d/%d user_id=%d seat=%s "
                        "idempotency_key=%s retry=%s status=%d body=%r",
                        index,
                        len(attempts),
                        attempt.user.user_id,
                        attempt.seat,
                        attempt.idempotency_key,
                        attempt.is_retry,
                        response.status_code,
                        body,
                    )
                mark_completed()
                return AttemptResult(
                    attempt,
                    response.status_code,
                    body,
                    elapsed_ms=elapsed_ms,
                    request_index=index,
                    request_id=response.headers.get("X-Request-ID"),
                    retry_attempts=retry_attempts,
                    started_offset_seconds=started_offset_seconds,
                    completed_offset_seconds=time.perf_counter() - started_at,
                )

    results = await asyncio.gather(
        *(
            send(index, attempt)
            for index, attempt in enumerate(attempts, start=first_index)
        )
    )
    return results, peak_in_flight


def _send_chunk(
    base_url: str,
    timeout: float,
    show_id: int,
    attempts: list[Attempt],
    concurrency: int,
    retry_deadline_seconds: float,
    started_at: float,
    first_index: int,
) -> tuple[list[AttemptResult], int]:
    async def run() -> tuple[list[AttemptResult], int]:
        limits = httpx.Limits(
            max_connections=concurrency,
            max_keepalive_connections=min(concurrency, 1_000),
        )
        async with httpx.AsyncClient(
            base_url=base_url, limits=limits, timeout=timeout
        ) as client:
            return await send_attempts(
                client,
                show_id,
                attempts,
                concurrency,
                retry_deadline_seconds=retry_deadline_seconds,
                started_at=started_at,
                first_index=first_index,
            )

    return asyncio.run(run())


async def send_attempts_in_processes(
    base_url: str,
    timeout: float,
    show_id: int,
    attempts: list[Attempt],
    concurrency: int,
    retry_deadline_seconds: float,
    processes: int,
    started_at: float,
    chunk_runner=_send_chunk,
) -> tuple[list[AttemptResult], int]:
    chunk_count = max(1, min(processes, len(attempts), concurrency))
    chunk_size = math.ceil(len(attempts) / chunk_count)
    chunks = [
        (offset, attempts[offset : offset + chunk_size])
        for offset in range(0, len(attempts), chunk_size)
    ]
    base_concurrency, extra = divmod(concurrency, len(chunks))
    loop = asyncio.get_running_loop()
    with ProcessPoolExecutor(
        max_workers=len(chunks), mp_context=multiprocessing.get_context("spawn")
    ) as executor:
        outcomes = await asyncio.gather(
            *(
                loop.run_in_executor(
                    executor,
                    chunk_runner,
                    base_url,
                    timeout,
                    show_id,
                    chunk,
                    base_concurrency + (1 if position < extra else 0),
                    retry_deadline_seconds,
                    started_at,
                    offset + 1,
                )
                for position, (offset, chunk) in enumerate(chunks)
            )
        )
    results = [result for chunk_results, _ in outcomes for result in chunk_results]
    # Per-process peaks may not coincide, so this is an upper bound on true peak.
    return results, sum(peak for _, peak in outcomes)


def _retry_delay_seconds(
    retry_attempts: int,
    remaining_seconds: float,
    *,
    retry_after_ms: object = None,
    retry_after_header: str | None = None,
) -> float:
    minimum_delay = 0.05
    if isinstance(retry_after_ms, (int, float)) and not isinstance(
        retry_after_ms, bool
    ) and retry_after_ms > 0:
        minimum_delay = max(minimum_delay, retry_after_ms / 1000)
    elif retry_after_header is not None:
        try:
            minimum_delay = max(minimum_delay, float(retry_after_header))
        except ValueError:
            pass

    jitter_cap = min(0.25, 0.05 * (2**retry_attempts))
    return min(
        minimum_delay + random.uniform(0, jitter_cap),
        remaining_seconds,
    )


async def run_burst(
    arguments: argparse.Namespace,
    users: list[BurstUser],
    admin_token: str,
) -> int:
    base_url = arguments.base_url.rstrip("/")
    limits = httpx.Limits(
        max_connections=arguments.concurrency,
        max_keepalive_connections=min(arguments.concurrency, 1_000),
    )
    attempts = build_attempts(
        users,
        arguments.seats,
        arguments.requests,
        arguments.retry_percent,
        arguments.seed,
    )
    retry_count = sum(attempt.is_retry for attempt in attempts)
    unique_key_count = len({attempt.idempotency_key for attempt in attempts})

    async with httpx.AsyncClient(
        base_url=base_url,
        limits=limits,
        timeout=arguments.timeout,
    ) as client:
        show_response = await client.post(
            "/shows",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": f"burst-{uuid4().hex[:12]}",
                "seats": arguments.seats,
                "price_paise": 25_000,
            },
        )
        if show_response.status_code != 201:
            raise RuntimeError(
                f"show creation returned {show_response.status_code}: "
                f"{show_response.text[:300]}"
            )
        show_state = show_response.json()
        show_id = int(show_state["id"])

        print(
            f"Created fresh show {show_id}; sending {len(attempts):,} requests "
            f"({unique_key_count:,} unique keys, {retry_count:,} retries) "
            f"from {len(users):,} users; concurrency cap "
            f"{arguments.concurrency:,} across {arguments.processes} "
            f"client process(es).",
            flush=True,
        )
        burst_started_at = time.perf_counter()
        sample_stop = asyncio.Event()
        sample_task = asyncio.create_task(
            _sample_mysql(
                sample_stop,
                arguments.db_sample_interval,
                burst_started_at,
            )
        )
        if arguments.processes > 1:
            results, peak_in_flight = await send_attempts_in_processes(
                base_url,
                arguments.timeout,
                show_id,
                attempts,
                arguments.concurrency,
                arguments.retry_deadline,
                arguments.processes,
                burst_started_at,
            )
        else:
            results, peak_in_flight = await send_attempts(
                client,
                show_id,
                attempts,
                arguments.concurrency,
                retry_deadline_seconds=arguments.retry_deadline,
                started_at=burst_started_at,
            )
        sample_stop.set()
        mysql_samples = await sample_task
        elapsed_seconds = time.perf_counter() - burst_started_at

        final_response = await client.get(f"/shows/{show_id}")
        if final_response.status_code != 200:
            raise RuntimeError(
                f"final show read returned {final_response.status_code}: "
                f"{final_response.text[:300]}"
            )
        final_state = final_response.json()

    exit_code = report_results(results, final_state, arguments.seats, peak_in_flight)
    _write_metrics(
        arguments.metrics_file,
        uuid4().hex,
        arguments,
        results,
        mysql_samples,
        peak_in_flight,
        elapsed_seconds,
        final_state,
        exit_code,
    )
    print(f"Analysis log: {arguments.metrics_file}")
    return exit_code


def report_results(
    results: list[AttemptResult],
    final_state: dict[str, object],
    seats: list[str],
    peak_in_flight: int,
) -> int:
    status_counts = Counter()
    declines = Counter()
    result_groups = defaultdict(list)
    reservations = {}
    transport_errors = Counter()
    malformed_successes = 0
    retry_attempts = sum(result.retry_attempts for result in results)
    retry_recoveries = sum(
        result.retry_attempts > 0 and result.status_code == 201
        for result in results
    )

    for result in results:
        if result.error:
            transport_errors[result.error] += 1
            continue
        status = result.status_code
        status_counts[status] += 1
        if status == 409:
            body = result.body
            detail = body.get("detail") if isinstance(body, dict) else None
            code = detail.get("code") if isinstance(detail, dict) else None
            declines[str(code or "unknown")] += 1
            if code not in {"hold_in_progress", "reservation_retry"}:
                result_groups[result.attempt.idempotency_key].append(result)
        elif status == 201:
            result_groups[result.attempt.idempotency_key].append(result)
            body = result.body
            if not isinstance(body, dict) or "reservation_id" not in body:
                malformed_successes += 1
            else:
                reservations[int(body["reservation_id"])] = body

    mismatched_replay_keys = sum(
        len({_stable_response(result) for result in grouped_results}) > 1
        for grouped_results in result_groups.values()
        if len(grouped_results) > 1
    )
    seat_winners = defaultdict(set)
    user_reservations = Counter()
    for reservation_id, body in reservations.items():
        user_reservations[int(body["user_id"])] += 1
        for seat in body.get("seats", []):
            seat_winners[str(seat)].add(reservation_id)
    duplicate_seat_winners = {
        seat: ids for seat, ids in seat_winners.items() if len(ids) > 1
    }
    limit_violations = {
        user_id: count for user_id, count in user_reservations.items() if count > 4
    }

    status_names = {201: "201 confirmed responses", 409: "409 declines"}
    print(f"Peak in-flight requests: {peak_in_flight:,}")
    for status, count in sorted(status_counts.items(), key=lambda item: item[0] or 0):
        name = status_names.get(status, f"HTTP {status}")
        print(f"{name}: {count:,}")
    print(f"Unique reservations: {len(reservations):,}")
    if declines:
        print("Declines by reason:")
        for reason, count in sorted(declines.items()):
            print(f"  {reason}: {count:,}")
    print(f"5xx responses: {sum(count for status, count in status_counts.items() if status >= 500):,}")
    print(f"Transport errors: {sum(transport_errors.values()):,}")
    for error_type, count in sorted(transport_errors.items()):
        print(f"  {error_type}: {count:,}")
    print(f"Mismatched same-key outcomes: {mismatched_replay_keys:,}")
    print(f"Retry attempts: {retry_attempts:,}")
    print(f"Retry recoveries: {retry_recoveries:,}")

    counts = final_state.get("counts", {})
    total_seats = final_state.get("total_seats")
    if not isinstance(counts, dict) or not isinstance(total_seats, int):
        print("Final show response has an invalid shape.")
        return 1
    count_sum = sum(int(value) for value in counts.values())
    held_count = int(counts.get("held", -1))
    reconciled = count_sum == total_seats and held_count == 0
    print(
        "Final seats: "
        f"available={counts.get('available')}, "
        f"held={counts.get('held')}, confirmed={counts.get('confirmed')}, "
        f"total={total_seats}; reconciliation={'PASS' if reconciled else 'FAIL'}"
    )

    no_unexpected_statuses = all(status in (201, 409) for status in status_counts)
    all_targeted_seats_present = all(seat in seat_winners for seat in seats)
    passed = (
        not any(status >= 500 for status in status_counts if status is not None)
        and not transport_errors
        and no_unexpected_statuses
        and not malformed_successes
        and not mismatched_replay_keys
        and not duplicate_seat_winners
        and not limit_violations
        and reconciled
        and all_targeted_seats_present
    )
    if duplicate_seat_winners:
        print(f"Seats with multiple unique reservations: {sorted(duplicate_seat_winners)}")
    if limit_violations:
        print(f"Users over the four-seat limit: {sorted(limit_violations)}")
    if not all_targeted_seats_present:
        print("At least one configured hot seat had no successful reservation.")
    print(f"Burst result: {'PASS' if passed else 'FAIL'}")
    return 0 if passed else 1


def _parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create a fresh show and run an async reservation burst."
    )
    parser.add_argument(
        "--base-url",
        default=env("TICKFAST_API_URL") or "http://127.0.0.1:8000",
        help="TickFast API base URL",
    )
    parser.add_argument(
        "--tokens-file",
        type=Path,
        default=Path.home() / ".tickfast" / "burst-user-tokens.txt",
        help="private token file; refreshed automatically (default: ~/.tickfast/burst-user-tokens.txt)",
    )
    parser.add_argument("--users", type=int, default=500)
    parser.add_argument(
        "--allow-non-test-database",
        action="store_true",
        help="allow automatic user creation when DB_DATABASE does not end in _test",
    )
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS)
    parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    parser.add_argument(
        "--processes",
        type=int,
        default=DEFAULT_PROCESSES,
        help="client processes sharing the concurrency cap; one process saturates a CPU core",
    )
    parser.add_argument("--retry-percent", type=float, default=5.0)
    parser.add_argument("--seats", nargs="+", default=list(DEFAULT_SEATS))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=float, default=60.0)
    parser.add_argument(
        "--retry-deadline",
        type=float,
        default=60.0,
        help="total time budget per attempt, including same-key retries",
    )
    parser.add_argument(
        "--metrics-file",
        type=Path,
        default=Path("burst-metrics.jsonl"),
        help="append request, MySQL sample, and summary events as JSON Lines",
    )
    parser.add_argument("--db-sample-interval", type=float, default=2.0)
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="log burst progress and detailed request failures",
    )
    arguments = parser.parse_args()

    if arguments.requests < 1 or arguments.concurrency < 1:
        parser.error("--requests and --concurrency must be positive")
    if arguments.processes < 1:
        parser.error("--processes must be positive")
    if arguments.users < 2:
        parser.error("--users must be at least 2")
    if not 0 <= arguments.retry_percent <= 50:
        parser.error("--retry-percent must be between 0 and 50")
    if arguments.timeout <= 0:
        parser.error("--timeout must be positive")
    if arguments.retry_deadline <= 0:
        parser.error("--retry-deadline must be positive")
    if arguments.db_sample_interval <= 0:
        parser.error("--db-sample-interval must be positive")
    arguments.seats = [seat.strip() for seat in arguments.seats]
    if (
        any(not seat or len(seat) > 255 for seat in arguments.seats)
        or len(set(arguments.seats)) != len(arguments.seats)
    ):
        parser.error("--seats must contain unique, nonblank labels of at most 255 characters")
    return arguments


def main() -> int:
    arguments = _parse_arguments()
    if arguments.verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        )
        LOGGER.setLevel(logging.DEBUG)
        logging.getLogger("httpx").setLevel(logging.WARNING)
        logging.getLogger("httpcore").setLevel(logging.WARNING)
    try:
        users, admin_token = prepare_credentials(
            arguments.tokens_file,
            arguments.users,
            arguments.allow_non_test_database,
        )
        print(
            f"Prepared {len(users):,} user tokens in {arguments.tokens_file}; "
            "admin JWT minted for this run.",
            flush=True,
        )
        return asyncio.run(run_burst(arguments, users, admin_token))
    except (
        AuthConfigurationError,
        DatabaseConfigurationError,
        PeeweeException,
        OSError,
        ValueError,
        httpx.HTTPError,
        RuntimeError,
    ) as exc:
        if arguments.verbose:
            LOGGER.exception("Burst failed")
            return 1
        print(f"burst failed ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())